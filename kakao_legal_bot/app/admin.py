"""The lawyer's review desk.

A deliberately plain HTML page: open the draft, edit it in a textarea,
approve, send. It is the human checkpoint between the model's output and
a client's inbox, so it stays boring on purpose — no JS framework, no
build step, nothing that can break between a draft and a deadline.
"""

from __future__ import annotations

import asyncio
import html
import time
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
 .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
          gap: 8px; margin: 12px 0; }}
 .stat {{ border: 1px solid rgba(128,128,128,.35); border-radius: 10px; padding: 10px 8px;
         text-align: center; }}
 .stat b {{ display: block; font-size: 22px; line-height: 1.3; }}
 .stat span {{ font-size: 12px; opacity: .75; }}
 h2 {{ font-size: 16px; margin: 26px 0 4px; }}
 .alert {{ border-color: rgba(220,80,60,.7); }}
 ul.plain {{ list-style: none; padding-left: 0; }}
 ul.plain li {{ padding: 6px 0; border-bottom: 1px solid rgba(128,128,128,.2); }}
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


def _ago(value: Any) -> str:
    """'3분 전' — a phone screen has no room for a timestamp."""
    try:
        seconds = time.time() - float(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 86400:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def _day_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


_INTAKE_LABELS = {
    "form_sent": "폼 보냄",
    "collecting": "사실관계 수집 중",
    "report_review": "보고서 확인 중",
    "quoted": "견적 안내함",
}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, services: Services = Depends(_require_token)
) -> HTMLResponse:
    """One page the lawyer opens on a phone: what needs me, right now.

    Ordered by what it costs to miss it — a client waiting on a draft first,
    a stalled intake next, the room list last.
    """
    token = request.query_params.get("token", "")
    snapshot = await asyncio.to_thread(services.db.dashboard_snapshot, _day_start())
    settings = services.settings

    def link(path: str) -> str:
        return f"{path}?token={quote(token)}"

    today = snapshot["today"]
    stats = "".join(
        f'<div class="stat"><b>{value}</b><span>{label}</span></div>'
        for label, value in (
            ("오늘 질문", today["questions"]),
            ("오늘 답변", today["answers"]),
            ("새 상담방", today["new_rooms"]),
            ("검토 대기", len(snapshot["waiting_review"])),
            ("작성 대기", len(snapshot["waiting_generation"])),
            ("진행 중 인테이크", len(snapshot["intakes"])),
        )
    )

    sections: list[str] = []

    def draft_cards(drafts: list[Any], empty: str, alert: bool = False) -> str:
        if not drafts:
            return f'<p class="note">{empty}</p>'
        cards = []
        for draft in drafts:
            note = ""
            if draft.status == "generation_failed":
                note = f'<div class="meta">직전 오류: {_esc(draft.last_error)}</div>'
            elif draft.status == "pending_generation":
                note = '<div class="meta">PC의 Codex 워커 대기 중 — PC가 켜져 있는지 확인해 주세요</div>'
            cards.append(
                f'<div class="card{" alert" if alert else ""}"><div class="row">'
                f"<strong>#{draft.id} {_esc(draft.title or draft.kind)}</strong>"
                f'<span class="badge">{_esc(draft.status)}</span></div>'
                f'<div class="meta">{_esc(draft.kind)} · 방 {_esc(draft.room_id)}'
                f' · 이메일 {_esc(draft.client_email) or "미등록"} · {_ago(draft.updated_at)}</div>'
                f"{note}"
                f'<a href="{link(f"/admin/drafts/{draft.id}")}">열어서 검토하기 →</a></div>'
            )
        return "".join(cards)

    sections.append("<h2>검토 대기 초안</h2>")
    sections.append(draft_cards(snapshot["waiting_review"], "검토하실 초안이 없습니다.", alert=True))

    if snapshot["failed"]:
        sections.append("<h2>작성 실패</h2>")
        sections.append(draft_cards(snapshot["failed"], "", alert=True))

    if snapshot["waiting_generation"]:
        sections.append("<h2>작성 대기</h2>")
        sections.append(draft_cards(snapshot["waiting_generation"], ""))

    if snapshot["approved"]:
        sections.append("<h2>승인됨 · 발송 전</h2>")
        sections.append(draft_cards(snapshot["approved"], ""))

    if snapshot["escalated"]:
        rows = "".join(
            f'<li><strong>{_esc(row["client_alias"] or row["room_id"])}</strong>'
            f'<div class="meta">상담번호 {row["id"]} · {_ago(row["updated_at"])}</div></li>'
            for row in snapshot["escalated"]
        )
        sections.append(f'<h2>변호사 확인 요청</h2><ul class="plain">{rows}</ul>')

    if snapshot["intakes"]:
        rows = "".join(
            f'<li><strong>{_esc(row["doc_kind"] or "문서")}</strong> · '
            f'{_esc(row["case_type"] or "사건유형 미정")}'
            f'{" (형사)" if str(row["track"]) == "criminal" else ""}'
            f'<div class="meta">{_INTAKE_LABELS.get(str(row["status"]), str(row["status"]))}'
            f' · 방 {_esc(row["room_id"])} · {_ago(row["updated_at"])}</div></li>'
            for row in snapshot["intakes"]
        )
        sections.append(f'<h2>진행 중 인테이크</h2><ul class="plain">{rows}</ul>')

    room_rows = []
    for row in snapshot["rooms"]:
        flags = []
        if row["muted"]:
            flags.append("정지")
        if row["lawyer_takeover"]:
            flags.append("변호사 개입")
        last = " ".join(str(row["last_text"] or "").split())[:70]
        speaker = {"user": "상담자", "bot": settings.bot_name, "lawyer": "변호사"}.get(
            str(row["last_role"] or ""), ""
        )
        room_rows.append(
            f'<li><strong>{_esc(row["room_name"] or row["room_id"])}</strong>'
            f'{" · " + " · ".join(flags) if flags else ""}'
            f'<div class="meta">{_esc(speaker)}: {_esc(last)} · {_ago(row["updated_at"])}</div></li>'
        )
    sections.append(
        f'<h2>상담방 ({len(snapshot["rooms"])})</h2>'
        f'<ul class="plain">{"".join(room_rows) or "<li>아직 없습니다.</li>"}</ul>'
    )

    queues = (
        f'<p class="meta">전송 대기 {snapshot["outbox_depth"]}건 · '
        f'초안 전체 {sum(snapshot["drafts_by_status"].values())}건 · '
        f'느린 답변(90초 초과) 오늘 {today["slow_answers"]}건</p>'
    )
    body = (
        f"<h1>{_esc(settings.lawyer_name)}님 업무 현황</h1>"
        f'<p class="meta">{time.strftime("%Y-%m-%d %H:%M")} 기준 · '
        f'<a href="{link("/admin")}">새로고침</a> · '
        f'<a href="{link("/admin/drafts")}">초안 전체</a></p>'
        f'<div class="stats">{stats}</div>'
        f"{''.join(sections)}"
        f"{queues}"
    )
    return HTMLResponse(_PAGE.format(title="업무 현황", body=body))


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
