from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from ..agent_service import AgentRunService
from ..auth import Principal, company_scope, current_principal


router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


class RunCreate(BaseModel):
    run_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    company_id: str | None = None


def _scope(principal: Principal, header: str | None, body_company: str | None = None) -> str:
    return company_scope(principal, body_company or header)


@router.get("")
def list_runs(request: Request, principal: Principal = Depends(current_principal),
              x_company_id: str | None = Header(default=None),
              run_type: str | None = Query(default=None, alias="type"),
              status: str | None = Query(default=None)):
    values = request.app.state.runs.list(_scope(principal, x_company_id))
    if run_type:
        values = [value for value in values if value.get("run_type") == run_type]
    if status:
        values = [value for value in values if value.get("status") == status]
    return values


@router.post("", status_code=201)
def create_run(body: RunCreate, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id, body.company_id)
    run = request.app.state.runs.create(company_id, body.run_type, body.payload, body.idempotency_key)
    request.app.state.db.activity(company_id, principal.id, "agent_run_created", "agent_run", run["id"])
    return run


@router.get("/{run_id}")
def get_run(run_id: str, request: Request, principal: Principal = Depends(current_principal),
            x_company_id: str | None = Header(default=None)):
    return request.app.state.runs.get(_scope(principal, x_company_id), run_id)


@router.post("/{run_id}/start", status_code=202)
def start_run(run_id: str, request: Request, principal: Principal = Depends(current_principal),
              x_company_id: str | None = Header(default=None)):
    return request.app.state.runs.start(_scope(principal, x_company_id), run_id)


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return request.app.state.runs.cancel(_scope(principal, x_company_id), run_id)


@router.post("/{run_id}/retry", status_code=201)
def retry_run(run_id: str, request: Request, principal: Principal = Depends(current_principal),
              x_company_id: str | None = Header(default=None)):
    return request.app.state.runs.retry(_scope(principal, x_company_id), run_id)


@router.get("/{run_id}/events")
def run_events(run_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return request.app.state.runs.events(_scope(principal, x_company_id), run_id)


@router.get("/{run_id}/logs")
def run_logs(run_id: str, request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None)):
    events = request.app.state.runs.events(_scope(principal, x_company_id), run_id)
    return [event for event in events if event["kind"] in {"log", "failed"}]

