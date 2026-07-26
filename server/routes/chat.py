from __future__ import annotations

import json
import queue

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Principal, company_scope, current_principal
from ..chat_bridge import TERMINAL_EVENTS


router = APIRouter(tags=["chat"])


class ChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(ChatModel):
    profile: str = Field(default="default", min_length=1, max_length=64)


class ChatStart(ChatModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=20_000)
    model: str = ""
    workspace: str = ""
    model_provider: str = ""
    profile: str = "default"


def _tenant(principal: Principal, company_header: str | None) -> str:
    return company_scope(principal, company_header)


@router.get("/api/session")
def get_session(
    request: Request,
    session_id: str = Query(min_length=1),
    messages: int = 0,
    resolve_model: int = 0,
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    del messages, resolve_model
    try:
        session = request.app.state.chat.get_session(
            session_id, _tenant(principal, x_company_id), principal.id,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"session": session}


@router.post("/api/session/new", status_code=201)
def new_session(
    body: SessionCreate,
    request: Request,
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    try:
        session = request.app.state.chat.create_session(
            _tenant(principal, x_company_id), principal.id, body.profile,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"session": session}


@router.post("/api/chat/start")
def start_chat(
    body: ChatStart,
    request: Request,
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "message must not be blank")
    try:
        stream_id = request.app.state.chat.start(
            body.session_id, _tenant(principal, x_company_id), principal.id, message,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"stream_id": stream_id}


@router.get("/api/chat/stream")
def stream_chat(request: Request, stream_id: str = Query(min_length=1)):
    try:
        stream = request.app.state.chat.claim_stream(stream_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    def events():
        try:
            while True:
                try:
                    event, payload = stream.events.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                yield f"event: {event}\ndata: {data}\n\n"
                if event in TERMINAL_EVENTS:
                    break
        finally:
            request.app.state.chat.abandon(stream_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
