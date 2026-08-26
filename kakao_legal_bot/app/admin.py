"""The lawyer's review desk.

A deliberately plain HTML page: open the draft, edit it in a textarea,
approve, send. It is the human checkpoint between the model's output and
a client's inbox, so it stays boring on purpose — no JS framework, no
build step, nothing that can break between a draft and a deadline.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .services import Services
from .workflows import send_draft

router = APIRouter(prefix="/admin", tags=["admin"])

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
        max-width: 860px; margin: 0 auto; padding: 24px 16px 64px; line-height: 1.6; }}
 h1 {{ font-size: 20px; }}
 a {{ color: inherit; }}
 .card {{ border: 1px solid rgba(128,128,128,.35); border-radius: 10px;
         padding: 14px 16px; margin: 12px 0; }}
 .meta {{ font-size: 13px; opacity: .75; }}
 .badge {{ display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px;
          border: 1px solid rgba(128,128,128,.5); }}
 textarea {{ width: 100%; min-height: 60vh; font-family: ui-monospace, monospace;
            font-size: 14px; padding: 10px; box-sizing: border-box; }}
 input[type=text] {{ width: 100%; padding: 8px; box-sizing: border-box; }}
 button {{ padding: 8px 16px; font-size: 15px; margin-right: 8px; cursor: pointer; }}
 .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
 .note {{ background: rgba(128,128,128,.12); padding: 8px 12px; border-radius: 8px;
         font-size: 14px; }}
</style></head><body>{body}</body></html>"""


def _require_token(request: Request) -> Services:
    services: Services = request.app.state.services
    expected = services.settings.admin_token
    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN 이 설정되지 않아 관리 화면이 비활성화되어 있습니다.")
    supplied = (
        request.query_params.get("token")
        or request.headers.get("x-admin-token")
        or request.cookies.get("moa_admin")
        or ""
    )
    if supplied != expected:
        raise HTTPException(401, "토큰이 올바르지 않습니다.")
    return services


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


@router.get("/drafts", response_class=HTMLResponse)
async def list_drafts(request: Request, services: Services = Depends(_require_token)) -> HTMLResponse:
    token = request.query_params.get("token", "")
    status = request.query_params.get("status", "")
    drafts = await asyncio.to_thread(services.db.list_drafts, status, 100)

    rows = []
    for draft in drafts:
        preview = _esc(" ".join(draft.body.split())[:180])
        rows.append(
            f'<div class="card"><div class="row">'
            f'<strong>#{draft.id} {_esc(draft.title or draft.kind)}</strong>'
            f'<span class="badge">{_esc(draft.status)}</span></div>'
            f'<div class="meta">{_esc(draft.kind)} · 이메일 {_esc(draft.client_email) or "미등록"}'
            f' · 방 {_esc(draft.room_id)}</div>'
            f"<p>{preview}…</p>"
            f'<a href="/admin/drafts/{draft.id}?token={_esc(token)}">열어서 검토하기 →</a></div>'
        )
    if not rows:
        rows.append('<p class="note">초안이 없습니다.</p>')

    filters = " · ".join(
        f'<a href="/admin/drafts?token={_esc(token)}&status={value}">{label}</a>'
        for value, label in (
            ("", "전체"),
            ("pending_review", "검토대기"),
            ("approved", "승인됨"),
            ("sent", "발송완료"),
        )
    )
    body = f"<h1>문서 초안 검토</h1><p class='meta'>{filters}</p>{''.join(rows)}"
    return HTMLResponse(_PAGE.format(title="초안 목록", body=body))


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def show_draft(
    draft_id: int, request: Request, services: Services = Depends(_require_token)
) -> HTMLResponse:
    token = request.query_params.get("token", "")
    message = request.query_params.get("msg", "")
    draft = await asyncio.to_thread(services.db.get_draft, draft_id)
    if draft is None:
        raise HTTPException(404, "초안을 찾을 수 없습니다.")

    banner = f'<p class="note">{_esc(message)}</p>' if message else ""
    if draft.awaiting_worker:
        # Nothing to edit yet — showing an empty textarea here invites the
        # lawyer to type into a document the worker is about to overwrite.
        banner += (
            '<p class="note">PC의 Codex 워커가 이 문서를 작성하고 있습니다. '
            "작성이 끝나면 이 화면에서 검토·수정하실 수 있습니다."
            f"{' (PC가 켜져 있는지 확인해 주세요)' if draft.status == 'pending_generation' else ''}"
            "</p>"
            f'<p class="meta">작성 지시<br>{_esc(draft.instructions)}</p>'
        )
        if draft.last_error:
            banner += f'<p class="note">직전 오류: {_esc(draft.last_error)}</p>'
        return HTMLResponse(
            _PAGE.format(
                title=f"초안 #{draft.id}",
                body=(
                    f'<h1>#{draft.id} {_esc(draft.title)} '
                    f'<span class="badge">{_esc(draft.status)}</span></h1>'
                    f'<div class="meta">{_esc(draft.kind)} · 방 {_esc(draft.room_id)}</div>'
                    f"{banner}"
                    f'<p><a href="/admin/drafts?token={_esc(token)}">← 목록</a></p>'
                ),
            )
        )
    body = f"""
<h1>#{draft.id} {_esc(draft.title)} <span class="badge">{_esc(draft.status)}</span></h1>
<div class="meta">{_esc(draft.kind)} · 방 {_esc(draft.room_id)}</div>
{banner}
<form method="post" action="/admin/drafts/{draft.id}?token={_esc(token)}">
  <p><label>제목<br><input type="text" name="title" value="{_esc(draft.title)}"></label></p>
  <p><label>상담자 이메일<br>
     <input type="text" name="client_email" value="{_esc(draft.client_email)}"
            placeholder="hong@example.com"></label></p>
  <p><label>변호사 메모 (메일 본문에 포함)<br>
     <input type="text" name="lawyer_note" value="{_esc(draft.lawyer_note)}"></label></p>
  <p><label>문서 본문<br><textarea name="body">{_esc(draft.body)}</textarea></label></p>
  <div class="row">
    <button name="action" value="save">저장</button>
    <button name="action" value="approve">저장 후 승인</button>
    <button name="action" value="send">저장·승인 후 이메일 발송</button>
  </div>
</form>
<p><a href="/admin/drafts?token={_esc(token)}">← 목록</a></p>"""
    return HTMLResponse(_PAGE.format(title=f"초안 #{draft.id}", body=body))


@router.post("/drafts/{draft_id}")
async def update_draft(
    draft_id: int,
    request: Request,
    services: Services = Depends(_require_token),
    action: str = Form("save"),
    title: str = Form(""),
    body: str = Form(""),
    client_email: str = Form(""),
    lawyer_note: str = Form(""),
) -> RedirectResponse:
    token = request.query_params.get("token", "")
    draft = await asyncio.to_thread(services.db.get_draft, draft_id)
    if draft is None:
        raise HTTPException(404, "초안을 찾을 수 없습니다.")

    fields: dict[str, Any] = {
        "title": title.strip(),
        "body": body,
        "client_email": client_email.strip(),
        "lawyer_note": lawyer_note.strip(),
    }
    if action in {"approve", "send"} and draft.status == "pending_review":
        fields["status"] = "approved"
    await asyncio.to_thread(services.db.update_draft, draft_id, **fields)

    message = "저장했습니다."
    if action == "approve":
        message = "승인했습니다."
    elif action == "send":
        ok, message = await send_draft(services, draft_id, override_email=client_email.strip())
        if ok:
            await services.sender.send(
                draft.room_id,
                f"요청하신 [{title.strip() or draft.title}] 문서를 이메일로 보내드렸습니다. 확인 부탁드립니다.",
                record_role="bot",
            )

    # quote(), not escape() — this goes in a query string, and a message
    # with spaces or parentheses silently disappears otherwise.
    return RedirectResponse(
        f"/admin/drafts/{draft_id}?token={quote(token)}&msg={quote(message)}", status_code=303
    )


@router.get("/rooms")
async def list_rooms(services: Services = Depends(_require_token)) -> JSONResponse:
    rooms = await asyncio.to_thread(services.db.list_rooms, 200)
    return JSONResponse([dict(row) for row in rooms])
