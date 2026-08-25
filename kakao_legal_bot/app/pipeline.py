"""From webhook to reply — including the KakaoTalk 5-second rule.

KakaoTalk gives a bot a few seconds to put *something* in the room before
the turn is treated as failed, and a real legal answer (retrieval + a law
API round-trip + generation) does not fit in that. So the answer runs as a
task and we race it against an acknowledgement deadline:

* answer wins  → send it, no placeholder, no double message
* deadline wins → send the placeholder, keep waiting, send the answer after

Everything slow that is *not* the reply (drafting, escalation, e-mail)
runs after the room already has text in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .commands import handle_command
from .db import Message, pseudonymise
from .iris import IrisEvent
from .services import Services
from .trigger import Action, decide
from .workflows import create_draft, notify_escalation

log = logging.getLogger(__name__)

INTRO_TEXT = """안녕하세요, {bot_name}입니다 🙂
{lawyer_name}님의 상담 채널에서 1차 안내를 맡고 있습니다.

상황을 편하게 적어주시면 관련 법령·판례를 찾아 정리해 드리고,
문서가 필요하시면 초안을 만들어 {lawyer_name}님 검토 후 이메일로 보내드립니다.
(제 답변은 일반적인 법률 정보이고, 최종 판단은 {lawyer_name}님이 확인해 드립니다.)"""

TIMEOUT_TEXT = """자료를 찾는 데 예상보다 오래 걸리고 있습니다.
{lawyer_name}님께 질문을 그대로 전달해 두었으니 확인 후 직접 답변드리겠습니다."""

ERROR_TEXT = """죄송합니다, 지금 답변을 만들지 못했습니다.
{lawyer_name}님께 전달해 두었습니다. 조금 뒤 다시 여쭤봐 주시면 답변드리겠습니다."""


class Pipeline:
    def __init__(self, services: Services) -> None:
        self.services = services
        self._last_answer_at: dict[str, float] = {}

    # ── entry point ──────────────────────────────────────────────────────
    async def handle(self, event: IrisEvent) -> None:
        try:
            await self._handle(event)
        except Exception:  # noqa: BLE001 — background task; never die silently
            log.exception("pipeline failed for room %s", event.room_id)

    async def _handle(self, event: IrisEvent) -> None:
        services = self.services
        settings = services.settings
        db = services.db
        room_id = event.room_id
        if not room_id:
            return

        kind = ""
        if event.is_direct_chat is True:
            kind = "direct"
        elif event.is_direct_chat is False:
            kind = "group"
        room = await asyncio.to_thread(db.upsert_room, room_id, event.room_name, kind)

        sender_key = pseudonymise(
            event.sender_id or event.sender_name, settings.pseudonym_salt
        )
        if event.is_text and event.text.strip():
            await asyncio.to_thread(
                db.add_message,
                room_id,
                "lawyer" if _is_lawyer_row(event, settings) else "user",
                event.text,
                event.sender_name if settings.store_raw_sender else "",
                sender_key,
                settings.history_turns,
            )

        decision = decide(
            event,
            settings,
            room_kind=str(room["kind"]),
            muted=bool(room["muted"]),
            lawyer_takeover=bool(room["lawyer_takeover"]),
        )
        log.info(
            "room=%s sender=%s action=%s reason=%s",
            room_id,
            sender_key or "?",
            decision.action.value,
            decision.reason,
        )

        if decision.action is Action.IGNORE:
            return
        if decision.action is Action.COMMAND:
            await handle_command(services, event, decision)
            return

        if not self._allow(room_id):
            log.info("room %s throttled", room_id)
            return
        if await self._over_daily_cap(room_id):
            await services.sender.send(
                room_id,
                f"오늘은 답변 한도를 채웠습니다. {settings.lawyer_name}님께 직접 문의해 주세요.",
                record_role="",
            )
            return

        # First contact in this room: the greeting also doubles as the fast
        # first message, so the room is never silent while we think.
        if not room["intro_sent"]:
            await asyncio.to_thread(db.set_room_flag, room_id, "intro_sent", 1)
            with contextlib.suppress(Exception):
                await services.sender.send(
                    room_id,
                    INTRO_TEXT.format(
                        bot_name=settings.bot_name, lawyer_name=settings.lawyer_name
                    ),
                    record_role="",
                )

        await self._answer(event, decision.question)

    # ── the 5-second race ────────────────────────────────────────────────
    async def _answer(self, event: IrisEvent, question: str) -> None:
        services = self.services
        settings = services.settings
        room_id = event.room_id
        started = time.monotonic()

        history = await asyncio.to_thread(
            services.db.recent_messages, room_id, settings.history_turns
        )
        history = _drop_current_question(history, event.text)

        task = asyncio.create_task(self._guarded_answer(question, history))

        # ① Kakao's window: put *something* in the room fast.
        ack_deadline = max(settings.ack_deadline_ms, 0) / 1000.0
        done, _pending = await asyncio.wait({task}, timeout=ack_deadline)
        if not done:
            # Bounded so a slow Iris cannot itself blow the budget.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(
                    services.sender.send(room_id, settings.ack_text, record_role=""),
                    timeout=max(1.0, ack_deadline),
                )

        # ② Still going after the first budget? Say how much longer and
        # keep the work — a legal answer often needs several law-API round
        # trips, and throwing away a nearly-finished one to apologise
        # serves nobody. Clients asking a legal question wait minutes.
        if settings.answer_extension_s > 0 and not done:
            first_wait = settings.answer_timeout_s - (time.monotonic() - started)
            if first_wait > 0:
                done, _pending = await asyncio.wait({task}, timeout=first_wait)
            if not done:
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        services.sender.send(
                            room_id, settings.patience_message(), record_role=""
                        ),
                        timeout=10.0,
                    )

        # ③ Hard ceiling. Only now do we hand the question to the lawyer.
        remaining = max(settings.total_answer_budget_s - (time.monotonic() - started), 1.0)
        try:
            result = await asyncio.wait_for(task, timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            await services.sender.send(
                room_id, TIMEOUT_TEXT.format(lawyer_name=settings.lawyer_name), record_role=""
            )
            await services.sender.notify_lawyer(
                f"⏱️ 답변 시간 초과 ({settings.total_answer_budget_s:.0f}초)\n"
                f"방: {event.room_name or room_id}\n질문: {question[:300]}"
            )
            return

        if result.error or not result.text:
            await services.sender.send(
                room_id, ERROR_TEXT.format(lawyer_name=settings.lawyer_name), record_role=""
            )
            await services.sender.notify_lawyer(
                f"⚠️ 답변 실패 ({result.error or 'empty answer'})\n"
                f"방: {event.room_name or room_id}\n질문: {question[:300]}"
            )
            return

        await services.sender.send(room_id, result.text)
        self._last_answer_at[room_id] = time.monotonic()

        await asyncio.to_thread(
            services.db.log_answer,
            room_id,
            question,
            result.text,
            pseudonymise(event.sender_id or event.sender_name, settings.pseudonym_salt),
            result.state.citations,
            result.tools_used,
            result.latency_ms,
        )

        # Follow-ups. Fire-and-forget: the client already has their answer.
        if result.state.draft_request or result.state.escalation:
            asyncio.create_task(self._follow_up(event, result))  # noqa: RUF006

    async def _guarded_answer(self, question: str, history: list[Message]):
        semaphore = self.services.semaphore
        if semaphore is None:
            return await self.services.agent.answer(question, history)
        async with semaphore:
            return await self.services.agent.answer(question, history)

    async def _follow_up(self, event: IrisEvent, result) -> None:  # noqa: ANN001
        services = self.services
        try:
            consultation = await asyncio.to_thread(
                services.db.get_or_create_consultation, event.room_id, event.sender_name
            )
            consult_id = int(consultation["id"])
            if result.state.escalation is not None:
                await notify_escalation(
                    services,
                    event.room_id,
                    event.room_name,
                    result.state.escalation,
                    consult_id,
                )
            if result.state.draft_request is not None:
                await create_draft(
                    services, event.room_id, result.state.draft_request, consult_id
                )
        except Exception:  # noqa: BLE001
            log.exception("follow-up failed for room %s", event.room_id)

    # ── throttling ───────────────────────────────────────────────────────
    def _allow(self, room_id: str) -> bool:
        cooldown = self.services.settings.room_cooldown_s
        if cooldown <= 0:
            return True
        last = self._last_answer_at.get(room_id)
        return last is None or (time.monotonic() - last) >= cooldown

    async def _over_daily_cap(self, room_id: str) -> bool:
        cap = self.services.settings.room_daily_cap
        if cap <= 0:
            return False
        since = time.time() - 86400
        count = await asyncio.to_thread(self.services.db.count_answers_since, room_id, since)
        return count >= cap


def _is_lawyer_row(event: IrisEvent, settings) -> bool:  # noqa: ANN001
    ids = {value.lower() for value in settings.lawyer_kakao_ids}
    if not ids:
        return False
    return event.sender_id.lower() in ids or event.sender_name.lower() in ids


def _drop_current_question(history: list[Message], text: str) -> list[Message]:
    """The inbound message is persisted before answering; don't repeat it."""
    if history and history[-1].text.strip() == (text or "").strip():
        return history[:-1]
    return history
