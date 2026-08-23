"""Slash commands — the lawyer's controls, handled without an LLM call."""

from __future__ import annotations

import asyncio

import pytest

from kakao_legal_bot.app.commands import handle_command
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.trigger import decide

from .conftest import FakeAgent, FakeSender, make_event


@pytest.fixture
def wiring(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변"),
        semaphore=asyncio.Semaphore(1),
    )
    return services, sender


async def run(services, event):
    decision = decide(event, services.settings, room_kind="direct")
    await handle_command(services, event, decision)
    return decision


@pytest.mark.asyncio
async def test_help_differs_for_the_lawyer(wiring):
    services, sender = wiring
    await run(services, make_event("/도움말"))
    await run(services, make_event("/도움말", sender_id="lawyer-uid"))

    assert "모아 사용법" in sender.texts[0]
    assert "변호사 전용" in sender.texts[1]


@pytest.mark.asyncio
async def test_email_registration_is_stored_on_the_consultation(wiring):
    services, sender = wiring
    services.db.upsert_room("room-1")
    await run(services, make_event("/이메일 hong@example.com"))

    consultation = services.db.get_or_create_consultation("room-1")
    assert consultation["client_email"] == "hong@example.com"
    assert "hong@example.com" in sender.texts[0]


@pytest.mark.asyncio
async def test_email_command_without_an_address_asks_again(wiring):
    services, sender = wiring
    services.db.upsert_room("room-1")
    await run(services, make_event("/이메일"))
    assert "이메일 주소를 함께" in sender.texts[0]


@pytest.mark.asyncio
async def test_client_can_call_the_lawyer(wiring):
    services, sender = wiring
    services.db.upsert_room("room-1")
    await run(services, make_event("/변호사 급합니다"))

    assert sender.lawyer_notes
    consultation = services.db.get_or_create_consultation("room-1")
    assert consultation["status"] == "awaiting_lawyer"


@pytest.mark.asyncio
async def test_takeover_and_release(wiring):
    services, _sender = wiring
    services.db.upsert_room("room-1")

    await run(services, make_event("/개입", sender_id="lawyer-uid"))
    assert services.db.get_room("room-1")["lawyer_takeover"] == 1

    await run(services, make_event("/복귀", sender_id="lawyer-uid"))
    assert services.db.get_room("room-1")["lawyer_takeover"] == 0


@pytest.mark.asyncio
async def test_auto_mode_marks_the_room_direct(wiring):
    services, _sender = wiring
    services.db.upsert_room("room-1", "상담방", "group")

    await run(services, make_event("/자동", sender_id="lawyer-uid"))
    assert services.db.get_room("room-1")["kind"] == "direct"

    await run(services, make_event("/수동", sender_id="lawyer-uid"))
    assert services.db.get_room("room-1")["kind"] == "group"


@pytest.mark.asyncio
async def test_draft_list_shows_pending_items(wiring):
    services, sender = wiring
    services.db.create_draft("room-1", "내용증명", "보증금 반환", "본문")
    await run(services, make_event("/초안", sender_id="lawyer-uid"))
    assert "보증금 반환" in sender.texts[0]


@pytest.mark.asyncio
async def test_approve_marks_the_draft_approved(wiring):
    services, sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "제목", "본문")

    await run(services, make_event(f"/승인 {draft_id}", sender_id="lawyer-uid"))

    assert services.db.get_draft(draft_id).status == "approved"
    assert f"#{draft_id} 승인" in sender.texts[0]


@pytest.mark.asyncio
async def test_send_refuses_an_unapproved_draft(wiring):
    services, sender = wiring
    draft_id = services.db.create_draft(
        "room-1", "내용증명", "제목", "본문", client_email="hong@example.com"
    )

    await run(services, make_event(f"/발송 {draft_id}", sender_id="lawyer-uid"))

    assert "승인 전" in sender.texts[0]
    assert services.db.get_draft(draft_id).status == "pending_review"


@pytest.mark.asyncio
async def test_send_refuses_without_a_client_email(wiring):
    services, sender = wiring
    draft_id = services.db.create_draft("room-1", "내용증명", "제목", "본문")
    services.db.update_draft(draft_id, status="approved")

    await run(services, make_event(f"/발송 {draft_id}", sender_id="lawyer-uid"))
    assert "이메일이 없습니다" in sender.texts[0]


@pytest.mark.asyncio
async def test_approve_with_a_bad_id_is_reported(wiring):
    services, sender = wiring
    await run(services, make_event("/승인 abc", sender_id="lawyer-uid"))
    assert "초안 번호" in sender.texts[0]


@pytest.mark.asyncio
async def test_status_reports_the_running_configuration(wiring):
    services, sender = wiring
    await run(services, make_event("/상태", sender_id="lawyer-uid"))
    assert "모아 상태" in sender.texts[0]
    assert services.settings.llm_model in sender.texts[0]
