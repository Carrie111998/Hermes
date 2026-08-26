"""Slash commands typed in the room.

Client side: ``/도움말``, ``/이메일 a@b.com``, ``/변호사``.
Lawyer side: ``/개입``, ``/조용``, ``/자동``, ``/초안``, ``/승인 12``, ``/발송 12``.

Commands are handled inline (no LLM call), so they always land well inside
the 5-second budget.
"""

from __future__ import annotations

import asyncio
import logging

from .iris import IrisEvent
from .services import Services
from .trigger import Decision, extract_email

log = logging.getLogger(__name__)

HELP_CLIENT = """[모아 사용법]
- 그냥 편하게 상황을 말씀해 주세요. 관련 법령·판례를 찾아 정리해 드립니다.
- /이메일 주소  → 문서를 받으실 이메일을 등록합니다.
- /변호사       → 담당 변호사에게 바로 연결합니다.

모아의 답변은 일반적인 법률 정보이고, 최종 법률판단은 담당 변호사가 확인 후 안내드립니다."""

HELP_LAWYER = """[변호사 전용 명령]
- /개입        이 방에서 모아를 조용히 시키고 직접 상담 (다시 /복귀)
- /조용        이 방에서 모아 완전 정지 (다시 /재개)
- /자동        이 방을 1:1 상담방으로 표시 — 호출 없이도 응답 (/수동 으로 해제)
- /초안        검토 대기 중인 초안 목록
- /초안 12     12번 초안 본문을 이 방으로 다시 보내기
- /승인 12     12번 초안 승인 (발송은 별도)
- /발송 12     12번 초안을 상담자 이메일로 발송
- /상태        서버 상태 · 업무 현황 링크"""


async def handle_command(services: Services, event: IrisEvent, decision: Decision) -> None:
    db = services.db
    sender = services.sender
    settings = services.settings
    room = event.room_id
    command, args = decision.command, decision.args.strip()

    async def reply(text: str) -> None:
        await sender.send(room, text, record_role="")

    # ── client commands ──────────────────────────────────────────────────
    if command == "help":
        await reply(HELP_LAWYER if decision.is_lawyer else HELP_CLIENT)
        return

    if command == "set_email":
        email = extract_email(args)
        if not email:
            await reply("이메일 주소를 함께 적어주세요. 예) /이메일 hong@example.com")
            return
        consultation = await asyncio.to_thread(db.get_or_create_consultation, room, event.sender_name)
        await asyncio.to_thread(db.update_consultation, int(consultation["id"]), client_email=email)
        await reply(f"{email} 로 등록했습니다. 문서는 변호사 검토 후 이 주소로 보내드립니다.")
        return

    if command == "escalate":
        consultation = await asyncio.to_thread(db.get_or_create_consultation, room, event.sender_name)
        await asyncio.to_thread(
            db.update_consultation, int(consultation["id"]), status="awaiting_lawyer"
        )
        await sender.notify_lawyer(
            f"🔔 상담자 요청으로 연결되었습니다.\n방: {event.room_name or room}\n"
            f"상담번호: {consultation['id']}\n메모: {args or '(없음)'}"
        )
        await reply(f"{settings.lawyer_name}님께 전달드렸습니다. 확인 후 이 방에서 직접 답변드릴 예정입니다.")
        return

    # ── lawyer commands ──────────────────────────────────────────────────
    if not decision.is_lawyer:
        return

    if command in {"takeover_on", "takeover_off"}:
        value = 1 if command == "takeover_on" else 0
        await asyncio.to_thread(db.set_room_flag, room, "lawyer_takeover", value)
        await reply("모아는 호출할 때만 답하겠습니다." if value else "모아가 다시 1차 응대를 맡습니다.")
        return

    if command in {"mute_on", "mute_off"}:
        value = 1 if command == "mute_on" else 0
        await asyncio.to_thread(db.set_room_flag, room, "muted", value)
        await reply("이 방에서 정지합니다." if value else "다시 응답합니다.")
        return

    if command in {"auto_on", "auto_off"}:
        kind = "direct" if command == "auto_on" else "group"
        await asyncio.to_thread(db.upsert_room, room, event.room_name, kind)
        await reply(
            "이 방을 1:1 상담방으로 등록했습니다. 이제 호출 없이도 답변합니다."
            if kind == "direct"
            else "이제 '모아'라고 불러야 답변합니다."
        )
        return

    if command == "draft_list":
        # "/초안 12" — send that document back into this room. The lawyer
        # scrolled past it, or wants to read it again on the phone.
        wanted = args.strip().lstrip("#")
        if wanted.isdigit():
            from .workflows import notify_draft_ready  # local import: avoids a cycle

            draft = await asyncio.to_thread(db.get_draft, int(wanted))
            if draft is None:
                await reply(f"#{wanted} 초안을 찾을 수 없습니다.")
            elif draft.awaiting_worker:
                await reply(f"#{wanted} 은 아직 작성 중입니다. ({draft.status})")
            else:
                await notify_draft_ready(services, int(wanted))
            return

        review = await asyncio.to_thread(db.list_drafts, "pending_review", 10)
        waiting = [
            draft
            for status in ("pending_generation", "generating", "generation_failed")
            for draft in await asyncio.to_thread(db.list_drafts, status, 10)
        ]
        if not review and not waiting:
            await reply("검토 대기 중인 초안이 없습니다.")
            return
        lines: list[str] = []
        if review:
            lines.append("[검토 대기]")
            for draft in review:
                lines.append(
                    f"#{draft.id} {draft.kind} · {draft.title} → {draft.client_email or '이메일 미등록'}"
                )
        if waiting:
            if lines:
                lines.append("")
            lines.append("[작성 대기 — PC 워커]")
            for draft in waiting:
                mark = "작성 실패" if draft.status == "generation_failed" else "대기 중"
                lines.append(f"#{draft.id} {draft.kind} · {draft.title} ({mark})")
        link = settings.admin_url("/drafts")
        if link:
            lines.append(f"\n검토·수정: {link}")
        lines.append("본문을 다시 보시려면 /초안 12 처럼 번호를 붙이세요.")
        await reply("\n".join(lines))
        return

    if command in {"draft_approve", "draft_send"}:
        head, _, note = args.partition(" ")
        try:
            draft_id = int(head.strip().lstrip("#"))
        except ValueError:
            await reply("초안 번호를 적어주세요. 예) /승인 12")
            return
        draft = await asyncio.to_thread(db.get_draft, draft_id)
        if draft is None:
            await reply(f"#{draft_id} 초안을 찾을 수 없습니다.")
            return

        if command == "draft_approve":
            await asyncio.to_thread(
                db.update_draft, draft_id, status="approved", lawyer_note=note.strip()
            )
            await reply(f"#{draft_id} 승인했습니다. 발송하려면 /발송 {draft_id}")
            return

        from .workflows import send_draft  # local import: avoids a cycle

        email = extract_email(note) or draft.client_email
        ok, message = await send_draft(services, draft_id, override_email=email)
        await reply(message)
        if ok:
            await sender.send(
                draft.room_id,
                f"요청하신 [{draft.title}] 문서를 {email} 로 보내드렸습니다. 확인 부탁드립니다.",
                record_role="bot",
            )
        return

    if command == "status":
        stats = services.rag.stats() if services.rag is not None else {}
        depth = await asyncio.to_thread(db.outbox_depth)
        pending = len(await asyncio.to_thread(db.list_drafts, "pending_review", 50))
        jobs = await asyncio.to_thread(db.draft_queue_depth)
        writer = "PC의 Codex 워커" if settings.draft_generator == "worker" else settings.draft_model
        await reply(
            "[모아 상태]\n"
            f"- 상담 응답: {settings.llm_model} ({settings.llm_provider})\n"
            f"- 문서 초안: {writer}\n"
            f"- 로컬 자료: 문서 {stats.get('documents', 0)} / 청크 {stats.get('chunks', 0)}\n"
            f"- 법령 API: {'연결됨' if services.law is not None else '미설정'}\n"
            f"- 전송 대기(outbox): {depth}\n"
            f"- 작성 대기 초안: {jobs}\n"
            f"- 검토 대기 초안: {pending}"
            + (f"\n\n업무 현황: {settings.admin_url()}" if settings.admin_url() else "")
        )
        return

    log.warning("unhandled command: %s", command)
