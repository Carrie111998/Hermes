"""Slow work that happens after the client already has a reply.

Document drafting takes a minute and a review round; escalation needs the
lawyer, who is asleep. Neither belongs on the message path, so both run
here as follow-ups once the room has been answered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .docxgen import build_docx, safe_filename
from .mailer import Attachment, MailError, send_email
from .services import Services
from .tools import DraftRequest, Escalation

log = logging.getLogger(__name__)

CLIENT_MAIL_TEMPLATE = """{client_greeting}

요청하신 [{title}] 문서를 첨부해 드립니다.
담당 {lawyer_name}이 검토·수정한 최종본입니다.

{note}
내용 중 수정이 필요하거나 궁금한 점이 있으시면 카카오톡 상담방으로 알려주세요.

{lawyer_name} 드림
"""


def _transcript(history: list[Any]) -> str:
    return "\n".join(
        f"{message.sender or message.role}: {message.text}" for message in history if message.text
    )


async def create_draft(
    services: Services, room_id: str, request: DraftRequest, consult_id: int | None = None
) -> int:
    """Queue or generate a document draft, then tell the lawyer about it."""
    db = services.db
    settings = services.settings
    history = await asyncio.to_thread(db.recent_messages, room_id, settings.history_turns)

    email = ""
    if consult_id is not None:
        consultation = await asyncio.to_thread(db.get_consultation, consult_id)
        if consultation is not None:
            email = str(consultation["client_email"] or "")

    if settings.draft_generator == "worker":
        return await _queue_for_worker(services, room_id, request, consult_id, email, history)
    return await _generate_here(services, room_id, request, consult_id, email, history)


async def _queue_for_worker(
    services: Services,
    room_id: str,
    request: DraftRequest,
    consult_id: int | None,
    email: str,
    history: list[Any],
) -> int:
    """Hand the job to the Codex worker running on the lawyer's own machine.

    The draft is stored with everything the worker needs, so the request
    survives a server restart and a laptop that is simply switched off.
    """
    draft_id = await asyncio.to_thread(
        services.db.create_draft,
        room_id,
        request.kind,
        request.title,
        "",
        consult_id,
        email,
        "pending_generation",
        request.instructions,
        _transcript(history)[-8000:],
    )
    depth = await asyncio.to_thread(services.db.draft_queue_depth)
    await services.sender.notify_lawyer(
        f"📝 초안 요청 접수 #{draft_id} — {request.kind} · {request.title}\n"
        f"방: {room_id}\n"
        f"상담자 이메일: {email or '미등록'}\n"
        f"---\n{request.instructions[:300]}\n"
        f"PC의 Codex 워커가 작성합니다. 대기 중 {depth}건.\n"
        f"(PC가 꺼져 있으면 켜실 때 처리됩니다)"
    )
    return draft_id


async def _generate_here(
    services: Services,
    room_id: str,
    request: DraftRequest,
    consult_id: int | None,
    email: str,
    history: list[Any],
) -> int:
    try:
        body = await services.agent.draft_document(
            request.kind, request.title, request.instructions, history
        )
    except Exception as exc:  # noqa: BLE001 — a failed draft must not lose the request
        log.exception("draft generation failed")
        body = (
            f"[자동 초안 생성 실패: {exc}]\n\n"
            f"작성 지시:\n{request.instructions}\n\n"
            "변호사님이 직접 작성하셔야 합니다."
        )

    draft_id = await asyncio.to_thread(
        services.db.create_draft,
        room_id,
        request.kind,
        request.title,
        body,
        consult_id,
        email,
        "pending_review",
        request.instructions,
    )
    await notify_draft_ready(services, draft_id)
    return draft_id


async def notify_draft_ready(services: Services, draft_id: int) -> None:
    """Tell the lawyer a draft is sitting in the review queue."""
    draft = await asyncio.to_thread(services.db.get_draft, draft_id)
    if draft is None:
        return
    preview = " ".join(draft.body.split())[:300]
    link = (
        f"\n검토/수정: {services.settings.public_base_url}/admin/drafts/{draft_id}"
        if services.settings.public_base_url
        else ""
    )
    await services.sender.notify_lawyer(
        f"📄 초안 준비됨 #{draft_id} — {draft.kind} · {draft.title}\n"
        f"방: {draft.room_id}\n"
        f"상담자 이메일: {draft.client_email or '미등록'}\n"
        f"---\n{preview}…{link}\n"
        f"승인: /승인 {draft_id}   발송: /발송 {draft_id}"
    )


async def notify_escalation(
    services: Services, room_id: str, room_name: str, escalation: Escalation, consult_id: int | None
) -> None:
    if consult_id is not None:
        await asyncio.to_thread(
            services.db.update_consultation, consult_id, status="awaiting_lawyer"
        )
    await services.sender.notify_lawyer(
        f"🔔 변호사 확인 요청\n"
        f"방: {room_name or room_id}\n"
        f"사유: {escalation.reason}\n"
        f"---\n{escalation.summary[:800]}"
    )


async def send_draft(
    services: Services, draft_id: int, override_email: str = ""
) -> tuple[bool, str]:
    """E-mail an approved draft to the client. Returns ``(ok, message)``."""
    db = services.db
    settings = services.settings
    draft = await asyncio.to_thread(db.get_draft, draft_id)
    if draft is None:
        return False, f"#{draft_id} 초안을 찾을 수 없습니다."

    email = (override_email or draft.client_email).strip()
    if not email:
        return False, (
            f"#{draft_id} 상담자 이메일이 없습니다. "
            f"상담자에게 /이메일 을 안내하거나 /발송 {draft_id} a@b.com 형식으로 지정하세요."
        )

    # The whole point of the review step: nothing reaches a client's inbox
    # until the lawyer has looked at it.
    if draft.status not in {"approved", "sent"}:
        return False, f"#{draft_id} 은 아직 승인 전입니다. /승인 {draft_id} 후 발송하세요."

    document = build_docx(draft.title, draft.body)
    note = f"[변호사 메모] {draft.lawyer_note}\n" if draft.lawyer_note else ""
    body = CLIENT_MAIL_TEMPLATE.format(
        client_greeting="안녕하세요, 상담 주셔서 감사합니다.",
        title=draft.title,
        lawyer_name=settings.lawyer_name,
        note=note,
    )
    try:
        await send_email(
            settings,
            to=email,
            subject=f"[{settings.lawyer_name}] {draft.title}",
            body=body,
            attachments=[Attachment(filename=safe_filename(draft.title), content=document)],
        )
    except MailError as exc:
        log.warning("draft %s send failed: %s", draft_id, exc)
        return False, f"#{draft_id} 발송 실패: {exc}"

    import time

    await asyncio.to_thread(
        db.update_draft, draft_id, status="sent", sent_at=time.time(), client_email=email
    )
    return True, f"#{draft_id} 을 {email} 로 발송했습니다."
