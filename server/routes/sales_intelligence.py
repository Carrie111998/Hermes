from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..agent_service import validate_payload
from ..auth import Principal, company_scope, current_principal
from ..db import json_dump, json_load, new_id, now
from ..quality import canonical_linkedin_url, normalize_name, validate_contact_record


router = APIRouter(tags=["sales-intelligence"])


class CountrySelection(BaseModel):
    countries: list[str] = Field(min_length=1, max_length=5)


class ScanCreate(BaseModel):
    countries: list[str] = Field(min_length=1, max_length=5)
    product_ids: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    target_company_types: list[str] = Field(default_factory=list)
    max_leads_per_country: int = Field(default=50, ge=1, le=200)
    scan_depth: str = "standard"
    data_sources: list[str] = Field(default_factory=lambda: ["web"])
    contact_discovery_enabled: bool = True
    outreach_generation_enabled: bool = False


class LeadCreate(BaseModel):
    company_name: str = Field(min_length=1)
    website: str | None = None
    country: str | None = None
    city: str | None = None
    industry: str | None = None
    source: str = "manual"
    notes: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class LeadPatch(BaseModel):
    company_name: str | None = None
    website: str | None = None
    country: str | None = None
    status: str | None = None
    data: dict[str, Any] | None = None


class ResearchCompany(BaseModel):
    company_name: str
    website: str | None = None
    country: str | None = None


class BulkResearch(BaseModel):
    lead_ids: list[str] = Field(min_length=1, max_length=50)


class ContactCreate(BaseModel):
    lead_id: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ContactPatch(BaseModel):
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    status: str | None = None
    data: dict[str, Any] | None = None


class ContactDiscovery(BaseModel):
    lead_ids: list[str] = Field(min_length=1, max_length=50)
    buyer_roles: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda: ["email", "linkedin"])
    max_contacts_per_company: int = Field(default=5, ge=1, le=10)


COUNTRIES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AU": "Australia", "BE": "Belgium",
    "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China",
    "CO": "Colombia", "DE": "Germany", "DZ": "Algeria", "EG": "Egypt", "ES": "Spain",
    "FR": "France", "GB": "United Kingdom", "GH": "Ghana", "ID": "Indonesia",
    "IN": "India", "IQ": "Iraq", "IT": "Italy", "JO": "Jordan", "JP": "Japan",
    "KE": "Kenya", "KR": "South Korea", "KW": "Kuwait", "MA": "Morocco",
    "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria", "NL": "Netherlands",
    "NZ": "New Zealand", "OM": "Oman", "PE": "Peru", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
    "RO": "Romania", "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore",
    "TN": "Tunisia", "TR": "Türkiye", "UA": "Ukraine", "US": "United States",
    "VN": "Vietnam", "ZA": "South Africa",
}


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _lead(row) -> dict:
    data = json_load(row["data"], {})
    return {"id": row["id"], "company_id": row["company_id"], "scan_id": row["scan_id"],
            "company_name": row["company_name"], "website": row["website"],
            "country": row["country"], "status": row["status"],
            "do_not_contact": bool(row["do_not_contact"]), **data,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _contact(row) -> dict:
    return {"id": row["id"], "company_id": row["company_id"], "lead_id": row["lead_id"],
            "email": row["email"], "phone": row["phone"], "linkedin_url": row["linkedin_url"],
            "status": row["status"], "do_not_contact": bool(row["do_not_contact"]),
            "data": json_load(row["data"], {}), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


@router.get("/lead-map/countries")
def countries(request: Request, principal: Principal = Depends(current_principal),
              x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    selected = {row["country_code"] for row in request.app.state.db.all(
        "SELECT country_code FROM selected_countries WHERE company_id=?", (company_id,)
    )}
    preferences = request.app.state.db.one(
        "SELECT data FROM company_sections WHERE company_id=? AND section='market_preferences'", (company_id,)
    )
    prefs = json_load(preferences["data"], {}) if preferences else {}
    target = set(prefs.get("target_markets", []))
    blocked_research = set(prefs.get("no_research_markets", []))
    blocked_outreach = set(prefs.get("no_outreach_markets", []))
    codes = sorted(set(COUNTRIES) | selected | target | blocked_research | blocked_outreach)
    return [{"code": code, "name": COUNTRIES.get(code, code), "selected": code in selected,
             "target": code in target, "research_allowed": code not in blocked_research,
             "outreach_allowed": code not in blocked_outreach} for code in codes]


@router.get("/lead-map/countries/{country_code}")
@router.get("/lead-map/countries/{country_code}/summary")
def country_summary(country_code: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id, code = _scope(principal, x_company_id), country_code.upper()
    lead_count = request.app.state.db.one(
        "SELECT COUNT(*) AS n FROM leads WHERE company_id=? AND country=?", (company_id, code)
    )["n"]
    sent = request.app.state.db.one(
        "SELECT COUNT(*) AS n FROM outreach_messages m JOIN leads l ON l.id=m.lead_id "
        "WHERE m.company_id=? AND l.country=? AND m.status='sent'", (company_id, code)
    )["n"]
    return {"code": code, "name": COUNTRIES.get(code, code), "lead_count": lead_count,
            "sent_messages": sent}


@router.get("/lead-map/selected-countries")
def selected_countries(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [row["country_code"] for row in request.app.state.db.all(
        "SELECT country_code FROM selected_countries WHERE company_id=? ORDER BY country_code", (company_id,)
    )]


@router.post("/lead-map/selected-countries")
def select_countries(body: CountrySelection, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    normalized = list(dict.fromkeys(code.upper() for code in body.countries))
    if any(len(code) != 2 for code in normalized):
        raise HTTPException(422, "Countries must be ISO alpha-2 codes")
    with request.app.state.db.transaction() as conn:
        conn.execute("DELETE FROM selected_countries WHERE company_id=?", (company_id,))
        conn.executemany("INSERT INTO selected_countries VALUES(?,?,?)",
                         [(company_id, code, now()) for code in normalized])
    return normalized


@router.delete("/lead-map/selected-countries/{country_code}", status_code=204)
def unselect_country(country_code: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    request.app.state.db.execute("DELETE FROM selected_countries WHERE company_id=? AND country_code=?",
                                 (_scope(principal, x_company_id), country_code.upper()))


@router.get("/lead-scans")
def lead_scans(request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return [{"id": row["id"], "status": row["status"], "config": json_load(row["config"], {}),
             "run_id": row["run_id"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
            for row in request.app.state.db.all(
                "SELECT * FROM lead_scans WHERE company_id=? ORDER BY created_at DESC",
                (_scope(principal, x_company_id),),
            )]


@router.post("/lead-scans", status_code=201)
def create_scan(body: ScanCreate, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id, stamp = _scope(principal, x_company_id), now()
    validate_payload("lead_scan", body.model_dump(), request.app.state.db, company_id)
    scan_id = new_id("scan")
    request.app.state.db.execute("INSERT INTO lead_scans VALUES(?,?,?,?,?,?,?)",
                                 (scan_id, company_id, "draft", json_dump(body.model_dump()),
                                  None, stamp, stamp))
    return {"id": scan_id, "status": "draft", "config": body.model_dump(), "run_id": None}


def _scan(company_id: str, scan_id: str, request: Request):
    row = request.app.state.db.one("SELECT * FROM lead_scans WHERE id=? AND company_id=?",
                                   (scan_id, company_id))
    if not row:
        raise HTTPException(404, "Lead scan not found")
    return row


@router.get("/lead-scans/{scan_id}")
def get_scan(scan_id: str, request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None)):
    row = _scan(_scope(principal, x_company_id), scan_id, request)
    return {"id": row["id"], "status": row["status"], "config": json_load(row["config"], {}),
            "run_id": row["run_id"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.post("/lead-scans/{scan_id}/start", status_code=202)
def start_scan(scan_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    scan = _scan(company_id, scan_id, request)
    if scan["status"] not in {"draft", "failed", "cancelled"}:
        raise HTTPException(409, "Scan cannot be started from its current state")
    payload = {**json_load(scan["config"], {}), "scan_id": scan_id}
    run = request.app.state.runs.create(company_id, "lead_scan", payload)
    request.app.state.db.execute("UPDATE lead_scans SET status='running',run_id=?,updated_at=? WHERE id=?",
                                 (run["id"], now(), scan_id))
    return request.app.state.runs.start(company_id, run["id"])


@router.post("/lead-scans/{scan_id}/cancel")
def cancel_scan(scan_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    scan = _scan(company_id, scan_id, request)
    if not scan["run_id"]:
        request.app.state.db.execute("UPDATE lead_scans SET status='cancelled',updated_at=? WHERE id=?",
                                     (now(), scan_id))
        return get_scan(scan_id, request, principal, x_company_id)
    run = request.app.state.runs.cancel(company_id, scan["run_id"])
    request.app.state.db.execute("UPDATE lead_scans SET status='cancelled',updated_at=? WHERE id=?",
                                 (now(), scan_id))
    return run


@router.post("/lead-scans/{scan_id}/retry", status_code=202)
def retry_scan(scan_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return start_scan(scan_id, request, principal, x_company_id)


@router.get("/lead-scans/{scan_id}/results")
def scan_results(scan_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    _scan(company_id, scan_id, request)
    return [_lead(row) for row in request.app.state.db.all(
        "SELECT * FROM leads WHERE company_id=? AND scan_id=? ORDER BY created_at", (company_id, scan_id)
    )]


@router.get("/leads")
def leads(request: Request, principal: Principal = Depends(current_principal),
          x_company_id: str | None = Header(default=None),
          country: str | None = Query(default=None),
          status: str | None = Query(default=None),
          scan: str | None = Query(default=None),
          band: str | None = Query(default=None),
          q: str | None = Query(default=None)):
    company_id = _scope(principal, x_company_id)
    values = [_lead(row) for row in request.app.state.db.all(
        "SELECT * FROM leads WHERE company_id=? ORDER BY created_at DESC", (company_id,)
    )]
    for value in values:
        value["score"] = _score(value["id"], company_id, request)
    if country:
        values = [value for value in values if value["country"] == country.upper()]
    if status:
        values = [value for value in values if value["status"] == status]
    if scan:
        values = [value for value in values if value["scan_id"] == scan]
    if band:
        values = [value for value in values if _score_band(value["score"]["final_score"]) == band]
    if q:
        needle = q.casefold()
        values = [value for value in values if any(
            needle in str(value.get(field) or "").casefold()
            for field in ("company_name", "city", "industry", "website")
        )]
    return values


@router.post("/leads", status_code=201)
def create_lead(body: LeadCreate, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id, stamp, lead_id = _scope(principal, x_company_id), now(), new_id("lead")
    data = {**body.data, **body.model_dump(exclude={"company_name", "website", "country", "data"})}
    request.app.state.db.execute(
        "INSERT INTO leads(id,company_id,company_name,website,country,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (lead_id, company_id, body.company_name, body.website, body.country.upper() if body.country else None,
         json_dump(data), stamp, stamp),
    )
    request.app.state.db.activity(company_id, principal.id, "lead_created", "lead", lead_id)
    return get_lead(lead_id, request, principal, x_company_id)


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM leads WHERE id=? AND company_id=?",
                                   (lead_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Lead not found")
    return _lead(row)


@router.patch("/leads/{lead_id}")
def patch_lead(lead_id: str, body: LeadPatch, request: Request,
               principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM leads WHERE id=? AND company_id=?", (lead_id, company_id))
    if not row:
        raise HTTPException(404, "Lead not found")
    values = body.model_dump(exclude_unset=True)
    data = values.pop("data", None)
    if data is not None:
        values["data"] = json_dump({**json_load(row["data"], {}), **data})
    values["updated_at"] = now()
    request.app.state.db.execute(f"UPDATE leads SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                                 (*values.values(), lead_id))
    return get_lead(lead_id, request, principal, x_company_id)


@router.delete("/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM leads WHERE id=? AND company_id=?",
                                        (lead_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Lead not found")


def _research_run(lead_id: str, request: Request, principal: Principal, company_header: str | None):
    company_id = _scope(principal, company_header)
    get_lead(lead_id, request, principal, company_header)
    run = request.app.state.runs.create(company_id, "lead_research", {"lead_id": lead_id})
    return request.app.state.runs.start(company_id, run["id"])


@router.post("/leads/{lead_id}/research", status_code=202)
@router.post("/research/lead/{lead_id}", status_code=202)
def research_lead(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return _research_run(lead_id, request, principal, x_company_id)


@router.post("/research/company", status_code=202)
def research_company(body: ResearchCompany, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    lead = create_lead(LeadCreate(company_name=body.company_name, website=body.website,
                                  country=body.country, source="research_only"),
                       request, principal, x_company_id)
    return _research_run(lead["id"], request, principal, x_company_id)


@router.post("/research/bulk", status_code=202)
def research_bulk(body: BulkResearch, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return [_research_run(lead_id, request, principal, x_company_id) for lead_id in body.lead_ids]


@router.get("/research")
def list_research(request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return [{"id": row["id"], "lead_id": row["lead_id"], "status": row["status"],
             "insights": json_load(row["insights"], {}), "run_id": row["run_id"]}
            for row in request.app.state.db.all(
                "SELECT * FROM research WHERE company_id=? ORDER BY created_at DESC",
                (_scope(principal, x_company_id),),
            )]


@router.get("/research/{research_id}")
def get_research(research_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM research WHERE id=? AND company_id=?",
                                   (research_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Research not found")
    return {"id": row["id"], "lead_id": row["lead_id"], "status": row["status"],
            "insights": json_load(row["insights"], {}), "run_id": row["run_id"]}


@router.get("/research/lead/{lead_id}/insights")
def research_insights(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one(
        "SELECT * FROM research WHERE lead_id=? AND company_id=? AND status='succeeded' "
        "ORDER BY created_at DESC LIMIT 1", (lead_id, _scope(principal, x_company_id)),
    )
    return json_load(row["insights"], {}) if row else None


@router.post("/research/lead/{lead_id}/regenerate-insights", status_code=202)
def regenerate_insights(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                        x_company_id: str | None = Header(default=None)):
    return _research_run(lead_id, request, principal, x_company_id)


def _score_band(value: int) -> str:
    return "high" if value >= 75 else "mid" if value >= 50 else "low"


def _score(lead_id: str, company_id: str, request: Request) -> dict:
    row = request.app.state.db.one(
        "SELECT insights FROM research WHERE lead_id=? AND company_id=? AND status='succeeded' "
        "ORDER BY created_at DESC LIMIT 1", (lead_id, company_id),
    )
    inputs = json_load(row["insights"], {}).get("score_inputs", {}) if row else {}
    names = ["product_fit_score", "market_fit_score", "company_quality_score", "intent_signal_score",
             "contactability_score", "insight_quality_score", "source_confidence_score"]
    scores = {name: max(0, min(100, int(inputs.get(name, 0) or 0))) for name in names}
    scores["final_score"] = round(sum(scores.values()) / len(names))
    scores["explanation"] = inputs.get("explanation", "No research-backed explanation available")
    return scores


@router.get("/leads/{lead_id}/score")
@router.get("/leads/{lead_id}/score/explanation")
def lead_score(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    get_lead(lead_id, request, principal, x_company_id)
    return _score(lead_id, company_id, request)


@router.post("/leads/{lead_id}/score/recalculate")
def recalculate_score(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return lead_score(lead_id, request, principal, x_company_id)


@router.post("/leads/{lead_id}/find-contacts", status_code=202)
def find_lead_contacts(lead_id: str, request: Request,
                       principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    return discover_contacts(ContactDiscovery(lead_ids=[lead_id]), request, principal, x_company_id)


@router.post("/leads/{lead_id}/mark-do-not-contact", status_code=204)
def mark_lead_dnc(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("UPDATE leads SET do_not_contact=1,updated_at=? WHERE id=? AND company_id=?",
                                        (now(), lead_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Lead not found")


@router.post("/leads/{lead_id}/archive", status_code=204)
def archive_lead(lead_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("UPDATE leads SET status='archived',updated_at=? WHERE id=? AND company_id=?",
                                        (now(), lead_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Lead not found")


@router.get("/contacts")
def contacts(request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None),
             lead_id: str | None = Query(default=None),
             email_status: str | None = Query(default=None),
             q: str | None = Query(default=None)):
    values = [_contact(row) for row in request.app.state.db.all(
        "SELECT * FROM contacts WHERE company_id=? ORDER BY created_at DESC", (_scope(principal, x_company_id),)
    )]
    if lead_id:
        values = [value for value in values if value["lead_id"] == lead_id]
    if email_status:
        values = [value for value in values if value["status"] == email_status or (
            email_status == "unverified" and value["status"] == "active"
        )]
    if q:
        needle = q.casefold()
        values = [value for value in values if any(
            needle in str(value.get(field) or value["data"].get(field) or "").casefold()
            for field in ("email", "phone", "linkedin_url", "full_name", "name", "title")
        )]
    return values


@router.post("/contacts", status_code=201)
def create_contact(body: ContactCreate, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    record = body.model_dump()
    failures = validate_contact_record(record)
    if failures:
        raise HTTPException(422, {"message": "Invalid contact", "failures": failures})
    if body.lead_id:
        get_lead(body.lead_id, request, principal, x_company_id)
    contact_id, stamp = new_id("con"), now()
    linkedin = canonical_linkedin_url(body.linkedin_url)
    request.app.state.db.execute(
        "INSERT INTO contacts(id,company_id,lead_id,email,phone,linkedin_url,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (contact_id, company_id, body.lead_id, body.email, body.phone, linkedin,
         json_dump(body.data), stamp, stamp),
    )
    return get_contact(contact_id, request, principal, x_company_id)


@router.get("/contacts/{contact_id}")
def get_contact(contact_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM contacts WHERE id=? AND company_id=?",
                                   (contact_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Contact not found")
    return _contact(row)


@router.patch("/contacts/{contact_id}")
def patch_contact(contact_id: str, body: ContactPatch, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM contacts WHERE id=? AND company_id=?",
                                   (contact_id, company_id))
    if not row:
        raise HTTPException(404, "Contact not found")
    values = body.model_dump(exclude_unset=True)
    data = values.pop("data", None)
    if data is not None:
        values["data"] = json_dump({**json_load(row["data"], {}), **data})
    if values.get("linkedin_url"):
        values["linkedin_url"] = canonical_linkedin_url(values["linkedin_url"])
    candidate = {"email": values.get("email", row["email"]), "phone": values.get("phone", row["phone"]),
                 "linkedin_url": values.get("linkedin_url", row["linkedin_url"])}
    failures = validate_contact_record(candidate)
    if failures:
        raise HTTPException(422, {"message": "Invalid contact", "failures": failures})
    values["updated_at"] = now()
    request.app.state.db.execute(f"UPDATE contacts SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                                 (*values.values(), contact_id))
    return get_contact(contact_id, request, principal, x_company_id)


@router.delete("/contacts/{contact_id}", status_code=204)
def delete_contact(contact_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM contacts WHERE id=? AND company_id=?",
                                        (contact_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Contact not found")


@router.post("/contacts/discover", status_code=202)
def discover_contacts(body: ContactDiscovery, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    for lead_id in body.lead_ids:
        get_lead(lead_id, request, principal, x_company_id)
    run = request.app.state.runs.create(company_id, "contact_discovery", body.model_dump())
    return request.app.state.runs.start(company_id, run["id"])


@router.post("/contacts/{contact_id}/verify")
def verify_contact(contact_id: str, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    contact = get_contact(contact_id, request, principal, x_company_id)
    failures = validate_contact_record(contact)
    status_value = "verified" if not failures else "invalid"
    request.app.state.db.execute("UPDATE contacts SET status=?,updated_at=? WHERE id=?",
                                 (status_value, now(), contact_id))
    return {"contact_id": contact_id, "status": status_value, "failures": failures,
            "verification": "passive"}


@router.post("/contacts/{contact_id}/mark-do-not-contact", status_code=204)
def mark_contact_dnc(contact_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute(
        "UPDATE contacts SET do_not_contact=1,status='blocked',updated_at=? WHERE id=? AND company_id=?",
        (now(), contact_id, _scope(principal, x_company_id)),
    ):
        raise HTTPException(404, "Contact not found")
