from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..auth import Principal, company_scope, current_principal
from ..db import Database, json_dump, json_load, now
from ..schemas import DataPatch


router = APIRouter(prefix="/company", tags=["company"])


def _scope(principal: Principal, company_header: str | None) -> str:
    return company_scope(principal, company_header)


def _get_section(db: Database, company_id: str, section: str) -> dict:
    row = db.one("SELECT data,updated_at FROM company_sections WHERE company_id=? AND section=?",
                 (company_id, section))
    return {"company_id": company_id, "data": json_load(row["data"], {}) if row else {},
            "updated_at": row["updated_at"] if row else None}


def _put_section(db: Database, company_id: str, section: str, patch: dict) -> dict:
    current = _get_section(db, company_id, section)["data"]
    merged = {**current, **patch}
    db.execute(
        "INSERT INTO company_sections(company_id,section,data,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(company_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
        (company_id, section, json_dump(merged), now()),
    )
    return _get_section(db, company_id, section)


@router.get("/profile")
def profile(request: Request, principal: Principal = Depends(current_principal),
            x_company_id: str | None = Header(default=None)):
    return _get_section(request.app.state.db, _scope(principal, x_company_id), "profile")


@router.patch("/profile")
def patch_profile(body: DataPatch, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    result = _put_section(request.app.state.db, company_id, "profile", body.data)
    request.app.state.db.activity(company_id, principal.id, "company_profile_updated", "company", company_id)
    return result


@router.get("/positioning")
def positioning(request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return _get_section(request.app.state.db, _scope(principal, x_company_id), "positioning")


@router.patch("/positioning")
def patch_positioning(body: DataPatch, request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return _put_section(request.app.state.db, _scope(principal, x_company_id), "positioning", body.data)


@router.get("/sales-preferences")
def sales_preferences(request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return _get_section(request.app.state.db, _scope(principal, x_company_id), "sales_preferences")


@router.patch("/sales-preferences")
def patch_sales_preferences(body: DataPatch, request: Request,
                            principal: Principal = Depends(current_principal),
                            x_company_id: str | None = Header(default=None)):
    return _put_section(request.app.state.db, _scope(principal, x_company_id),
                        "sales_preferences", body.data)


@router.get("/email-templates")
def email_templates(request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    """Language-keyed outreach templates: {templates: {<lang>: {subject, body}}}."""
    return _get_section(request.app.state.db, _scope(principal, x_company_id), "email_templates")


@router.patch("/email-templates")
def patch_email_templates(body: DataPatch, request: Request,
                          principal: Principal = Depends(current_principal),
                          x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    result = _put_section(request.app.state.db, company_id, "email_templates", body.data)
    request.app.state.db.activity(company_id, principal.id, "email_templates_updated",
                                  "company", company_id)
    return result

