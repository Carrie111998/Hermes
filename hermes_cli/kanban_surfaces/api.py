"""FastAPI router factory for bounded Kanban security surfaces."""

from __future__ import annotations

from collections.abc import Callable

from .service import Actor, KanbanSecurityService

ActorResolver = Callable[[object], Actor]


def create_router(*, service: KanbanSecurityService, actor_resolver: ActorResolver):
    try:
        from fastapi import APIRouter, Body, Header, HTTPException, Request
    except ImportError as exc:  # pragma: no cover - dependency supplied by Hermes
        raise RuntimeError("FastAPI is required for the Kanban API surface") from exc

    router = APIRouter(prefix="/api/kanban/security", tags=["kanban-security"])

    def actor(request: Request) -> Actor:
        try:
            return actor_resolver(request)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/tasks/{task_id}/runs/{run_id}")
    def run_summary(task_id: str, run_id: int, request: Request):
        try:
            return service.run_summary(actor(request), task_id=task_id, run_id=run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @router.get("/tasks/{task_id}/events")
    def events(task_id: str, request: Request, cursor: str | None = None, limit: int = 100):
        try:
            return service.event_page(actor(request), task_id=task_id, cursor=cursor, limit=limit)
        except RuntimeError as exc:
            detail = {"code": "cursor_expired", "oldest_cursor": getattr(exc, "oldest_cursor", None)}
            raise HTTPException(status_code=410, detail=detail) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/publications")
    def publications(request: Request, limit: int = 100):
        return service.publication_queue(actor(request), limit=limit)

    @router.post("/publications/{intent_id}/decision")
    def decision(
        intent_id: str,
        request: Request,
        payload: dict = Body(...),
        if_match: str = Header(..., alias="If-Match"),
    ):
        wire = if_match.strip('"')
        if wire != payload.get("wire_sha256"):
            raise HTTPException(status_code=412, detail="If-Match does not bind payload digest")
        try:
            approval_id = service.approve(
                actor(request),
                intent_id=intent_id,
                wire_sha256=wire,
                decision=str(payload.get("decision")),
                reason=payload.get("reason"),
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"approval_id": approval_id, "intent_id": intent_id, "wire_sha256": wire}

    @router.get("/tasks/{task_id}/runs/{run_id}/evidence")
    def evidence(task_id: str, run_id: int, request: Request):
        return service.evidence_vector(actor(request), task_id=task_id, run_id=run_id)

    return router
