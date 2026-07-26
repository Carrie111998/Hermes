from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..auth import Principal, company_scope, current_principal
from ..db import Database, json_dump, json_load, now
from ..schemas import DataPatch
from .company import _put_section


router = APIRouter(prefix="/onboarding", tags=["onboarding"])
STEPS = {
    "company-identity": "profile",
    "positioning": "positioning",
    "products": "products",
    "internal-sales-data": "internal_sales_data",
    "current-contacts": "current_contacts",
    "target-markets": "market_preferences",
    "integrations": "integrations",
    "brain-review": "brain_review",
}
REQUIRED_STEPS = {
    "company-identity", "positioning", "products", "internal-sales-data", "target-markets",
}


def _company(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _status(db: Database, company_id: str) -> dict:
    row = db.one("SELECT * FROM onboarding WHERE company_id=?", (company_id,))
    if not row:
        db.execute("INSERT INTO onboarding(company_id,updated_at) VALUES(?,?)", (company_id, now()))
        row = db.one("SELECT * FROM onboarding WHERE company_id=?", (company_id,))
    return {
        "company_id": company_id, "status": row["status"], "current_step": row["current_step"],
        "completed_steps": json_load(row["completed_steps"], []),
        "started_at": row["started_at"], "completed_at": row["completed_at"],
    }


@router.get("/status")
def onboarding_status(request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return _status(request.app.state.db, _company(principal, x_company_id))


@router.post("/start")
def start_onboarding(request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _company(principal, x_company_id)
    stamp = now()
    request.app.state.db.execute(
        "INSERT INTO onboarding(company_id,status,current_step,started_at,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(company_id) DO UPDATE SET status='in_progress',"
        "current_step=COALESCE(onboarding.current_step,'company-identity'),"
        "started_at=COALESCE(onboarding.started_at,excluded.started_at),updated_at=excluded.updated_at",
        (company_id, "in_progress", "company-identity", stamp, stamp),
    )
    return _status(request.app.state.db, company_id)


def _patch_step(step: str, body: DataPatch, request: Request, principal: Principal,
                company_header: str | None) -> dict:
    company_id = _company(principal, company_header)
    db: Database = request.app.state.db
    _put_section(db, company_id, STEPS[step], body.data)
    state = _status(db, company_id)
    completed = list(dict.fromkeys([*state["completed_steps"], step]))
    step_names = list(STEPS)
    next_step = step_names[min(step_names.index(step) + 1, len(step_names) - 1)]
    db.execute("UPDATE onboarding SET status='in_progress',current_step=?,completed_steps=?,updated_at=? "
               "WHERE company_id=?", (next_step, json_dump(completed), now(), company_id))
    db.activity(company_id, principal.id, "onboarding_step_completed", "onboarding", company_id,
                {"step": step})
    return _status(db, company_id)


def _register_step(path: str):
    async def endpoint(
        body: DataPatch,
        request: Request,
        principal: Principal = Depends(current_principal),
        x_company_id: str | None = Header(default=None),
    ):
        return _patch_step(path, body, request, principal, x_company_id)
    router.add_api_route(f"/{path}", endpoint, methods=["PATCH"], name=f"onboarding_{path}")


for _step in STEPS:
    _register_step(_step)


@router.post("/complete")
def complete_onboarding(request: Request, principal: Principal = Depends(current_principal),
                        x_company_id: str | None = Header(default=None)):
    company_id = _company(principal, x_company_id)
    state = _status(request.app.state.db, company_id)
    # The original five PRODUCT.md steps remain the compatibility boundary.
    # WebUI convenience steps are persisted when used, but do not make older
    # API clients unable to complete onboarding.
    missing = [step for step in STEPS
               if step in REQUIRED_STEPS and step not in state["completed_steps"]]
    if missing:
        raise HTTPException(409, {"message": "Onboarding is incomplete", "missing_steps": missing})
    stamp = now()
    request.app.state.db.execute(
        "UPDATE onboarding SET status='completed',current_step=NULL,completed_at=?,updated_at=? WHERE company_id=?",
        (stamp, stamp, company_id),
    )
    request.app.state.db.activity(company_id, principal.id, "onboarding_completed", "onboarding", company_id)
    return _status(request.app.state.db, company_id)

