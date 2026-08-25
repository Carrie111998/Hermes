"""The KakaoTalk 5-second rule, and everything the pipeline decides.

The rule: a room must never sit silent while the model thinks. But a
placeholder on *every* turn is noise, so a fast answer must skip it.
"""

from __future__ import annotations

import asyncio

import pytest

from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.pipeline import Pipeline
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.tools import DraftRequest, Escalation

from .conftest import FakeAgent, FakeSender, make_event


def build(settings, db, agent) -> tuple[Services, FakeSender, Pipeline]:
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=agent,
        semaphore=asyncio.Semaphore(4),
    )
    return services, sender, Pipeline(services)


def ready_room(db, room_id: str = "room-1") -> None:
    """A room that has already been greeted and already had its first question.

    Most tests are about what happens on an ordinary follow-up turn, not
    about first contact, so they start from here.
    """
    db.upsert_room(room_id, "상담방", "direct")
    db.set_room_flag(room_id, "intro_sent", 1)
    db.set_room_flag(room_id, "first_alerts_done", 1)



@pytest.mark.asyncio
async def test_fast_answer_sends_no_placeholder(settings, db):
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent("바로 답변", delay=0.0))

    await pipeline.handle(make_event("전세금 질문이요"))

    assert sender.texts == ["바로 답변"]


@pytest.mark.asyncio
async def test_slow_answer_gets_a_placeholder_first(settings, db):
    ready_room(db)
    # ACK_DEADLINE_MS is 100ms in the test settings.
    _services, sender, pipeline = build(settings, db, FakeAgent("느린 답변", delay=0.4))

    await pipeline.handle(make_event("복잡한 질문이요"))

    assert len(sender.texts) == 2
    assert sender.texts[0] == settings.ack_text
    assert sender.texts[1] == "느린 답변"


@pytest.mark.asyncio
async def test_first_message_in_a_room_gets_the_intro(settings, db):
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    await pipeline.handle(make_event("안녕하세요", direct=True))

    assert "모아입니다" in sender.texts[0]
    assert sender.texts[-1] == "답변"
    assert db.get_room("room-1")["intro_sent"] == 1


@pytest.mark.asyncio
async def test_intro_is_sent_only_once(settings, db):
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    await pipeline.handle(make_event("첫 질문", direct=True, log_id="a"))
    await pipeline.handle(make_event("둘째 질문", direct=True, log_id="b"))

    assert sum("모아입니다" in text for text in sender.texts) == 1


@pytest.mark.asyncio
async def test_slow_answer_buys_more_time_instead_of_giving_up(settings, db):
    """The first budget expiring is a status update, not a failure.

    A legal answer that needs several law-API round trips routinely runs
    past 90s; throwing that work away to apologise would be the worst of
    both worlds.
    """
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent("오래 걸린 답변", delay=0.5))
    object.__setattr__(settings, "answer_timeout_s", 0.25)
    object.__setattr__(settings, "answer_extension_s", 180.0)

    await pipeline.handle(make_event("아주 복잡한 질문"))

    assert sender.texts[0] == settings.ack_text
    assert sender.texts[1] == "답변을 생성하느라 시간이 걸리고 있습니다. 3분내로 답변드리도록 하겠습니다. 잠시만 기다려주세요."
    assert sender.texts[2] == "오래 걸린 답변"
    # Nobody woke the lawyer — the answer arrived on its own.
    assert sender.lawyer_notes == []


@pytest.mark.asyncio
async def test_the_patience_notice_says_the_configured_number_of_minutes(settings, db):
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent("답변", delay=0.5))
    object.__setattr__(settings, "answer_timeout_s", 0.25)
    object.__setattr__(settings, "answer_extension_s", 300.0)

    await pipeline.handle(make_event("질문"))

    assert "5분내로" in sender.texts[1]


@pytest.mark.asyncio
async def test_only_the_hard_ceiling_hands_the_question_to_the_lawyer(settings, db):
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent("영원히", delay=30))
    object.__setattr__(settings, "answer_timeout_s", 0.2)
    object.__setattr__(settings, "answer_extension_s", 0.3)

    await pipeline.handle(make_event("끝나지 않는 질문"))

    # The promise is made first… (the minute count itself is asserted above)
    assert "시간이 걸리고 있습니다" in sender.texts[1]
    assert any("예상보다 오래" in text for text in sender.texts)  # …then kept honest
    assert any("시간 초과" in note for note in sender.lawyer_notes)


@pytest.mark.asyncio
async def test_extension_can_be_turned_off(settings, db):
    """ANSWER_EXTENSION_S=0 restores the plain single-deadline behaviour."""
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent("영원히", delay=30))
    object.__setattr__(settings, "answer_timeout_s", 0.3)
    object.__setattr__(settings, "answer_extension_s", 0.0)

    await pipeline.handle(make_event("질문"))

    assert not any("시간이 걸리고 있습니다" in text for text in sender.texts)
    assert any("예상보다 오래" in text for text in sender.texts)
    assert sender.lawyer_notes


@pytest.mark.asyncio
async def test_llm_failure_falls_back_and_notifies(settings, db):
    ready_room(db)
    _services, sender, pipeline = build(settings, db, FakeAgent(""))

    await pipeline.handle(make_event("질문"))

    assert any("답변을 만들지 못했습니다" in text for text in sender.texts)
    assert sender.lawyer_notes


@pytest.mark.asyncio
async def test_ignored_message_costs_nothing(settings, db):
    db.upsert_room("room-1", "상담방", "group")
    db.set_room_flag("room-1", "intro_sent", 1)
    agent = FakeAgent("답변")
    _services, sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("둘이서 하는 얘기"))

    assert agent.calls == []
    assert sender.texts == []


@pytest.mark.asyncio
async def test_inbound_message_is_stored_but_not_repeated_in_the_prompt(settings, db):
    ready_room(db)
    db.add_message("room-1", "user", "이전 질문")
    db.add_message("room-1", "bot", "이전 답변")

    captured: list[list] = []

    class RecordingAgent(FakeAgent):
        async def answer(self, question, history):
            captured.append(list(history))
            return await super().answer(question, history)

    _services, _sender, pipeline = build(settings, db, RecordingAgent("답변"))
    await pipeline.handle(make_event("새 질문"))

    texts = [message.text for message in captured[0]]
    assert texts == ["이전 질문", "이전 답변"]
    assert "새 질문" in [m.text for m in db.recent_messages("room-1")]


@pytest.mark.asyncio
async def test_answers_are_logged_for_audit(settings, db):
    ready_room(db)
    agent = FakeAgent("답변", citations=["민법 제618조"], tools=["search_law"])
    _services, _sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("질문"))

    rows = db._query("SELECT * FROM answers")
    assert len(rows) == 1
    assert "민법 제618조" in rows[0]["citations"]
    assert "search_law" in rows[0]["tools_used"]
    assert rows[0]["sender_key"] and rows[0]["sender_key"] != "uid-1"


@pytest.mark.asyncio
async def test_daily_cap_stops_answering(settings, db):
    ready_room(db)
    object.__setattr__(settings, "room_daily_cap", 1)
    agent = FakeAgent("답변")
    _services, sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("첫 질문", log_id="a"))
    await pipeline.handle(make_event("둘째 질문", log_id="b"))

    assert agent.calls == ["첫 질문"]
    assert "한도를 채웠습니다" in sender.texts[-1]


@pytest.mark.asyncio
async def test_cooldown_drops_a_burst(settings, db):
    ready_room(db)
    object.__setattr__(settings, "room_cooldown_s", 60.0)
    agent = FakeAgent("답변")
    _services, _sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("연타1", log_id="a"))
    await pipeline.handle(make_event("연타2", log_id="b"))

    assert agent.calls == ["연타1"]


@pytest.mark.asyncio
async def test_first_question_alerts_the_lawyer_three_times(settings, db):
    """접수 → 진행 → 완료. The lawyer learns who applied before the answer exists."""
    _services, sender, pipeline = build(settings, db, FakeAgent("답변", delay=0.5))
    object.__setattr__(settings, "answer_timeout_s", 0.25)
    object.__setattr__(settings, "answer_extension_s", 180.0)

    await pipeline.handle(make_event("전세금을 못 받고 있어요", sender="홍길동", direct=True))
    await _settle()

    assert len(sender.lawyer_notes) == 3
    opened, progress, done = sender.lawyer_notes

    assert "[1/3]" in opened
    assert "홍길동" in opened  # 누가 신청했는지
    assert "전세금을 못 받고 있어요" in opened

    assert "[2/3]" in progress
    assert "3분 내 답변" in progress

    assert "[3/3]" in done
    assert "답변" in done
    assert "다음 질문부터는 알림을 보내지 않습니다" in done


@pytest.mark.asyncio
async def test_the_opening_alert_goes_out_before_the_answer_exists(settings, db):
    agent = FakeAgent("답변", delay=1.0)
    _services, sender, pipeline = build(settings, db, agent)

    task = asyncio.create_task(pipeline.handle(make_event("질문", direct=True)))
    # Well inside the answer's own latency.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sender.lawyer_notes:
            break

    assert sender.lawyer_notes and "[1/3]" in sender.lawyer_notes[0]
    assert sender.texts == [] or "모아입니다" in sender.texts[0]  # answer not sent yet
    await task


@pytest.mark.asyncio
async def test_a_fast_first_answer_still_gets_two_alerts(settings, db):
    """No 90-second mark to report — so 접수 and 완료 only, never a filler."""
    _services, sender, pipeline = build(settings, db, FakeAgent("바로 답변"))

    await pipeline.handle(make_event("간단한 질문", direct=True))
    await _settle()

    assert [note[:6] for note in sender.lawyer_notes] == ["🆕 [1/3", "✅ [3/3"]


@pytest.mark.asyncio
async def test_later_questions_in_the_same_room_are_silent(settings, db):
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    await pipeline.handle(make_event("첫 질문", direct=True, log_id="a"))
    await _settle()
    first_round = len(sender.lawyer_notes)
    sender.lawyer_notes.clear()

    await pipeline.handle(make_event("둘째 질문", direct=True, log_id="b"))
    await pipeline.handle(make_event("셋째 질문", direct=True, log_id="c"))
    await _settle()

    assert first_round == 2
    assert sender.lawyer_notes == []


@pytest.mark.asyncio
async def test_each_room_gets_its_own_alert_sequence(settings, db):
    """A second client is a second consultation — the lawyer must hear about it."""
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    await pipeline.handle(make_event("첫 상담자", room_id="room-1", direct=True, log_id="a"))
    await pipeline.handle(make_event("둘째 상담자", room_id="room-2", direct=True, log_id="b"))
    await _settle()

    openers = [note for note in sender.lawyer_notes if "[1/3]" in note]
    assert len(openers) == 2
    assert "첫 상담자" in openers[0]
    assert "둘째 상담자" in openers[1]


@pytest.mark.asyncio
async def test_a_timed_out_first_question_still_closes_the_sequence(settings, db):
    _services, sender, pipeline = build(settings, db, FakeAgent("영원히", delay=30))
    object.__setattr__(settings, "answer_timeout_s", 0.2)
    object.__setattr__(settings, "answer_extension_s", 0.3)

    await pipeline.handle(make_event("끝나지 않는 질문", direct=True))
    await _settle()

    assert len(sender.lawyer_notes) == 3
    assert "[3/3]" in sender.lawyer_notes[-1]
    assert "직접 답변이 필요합니다" in sender.lawyer_notes[-1]


@pytest.mark.asyncio
async def test_alerts_can_be_turned_off(settings, db):
    object.__setattr__(settings, "lawyer_first_turn_alerts", False)
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    await pipeline.handle(make_event("질문", direct=True))
    await _settle()

    assert sender.lawyer_notes == []
    assert sender.texts[-1] == "답변"


@pytest.mark.asyncio
async def test_a_failing_lawyer_alert_never_breaks_the_answer(settings, db):
    _services, sender, pipeline = build(settings, db, FakeAgent("답변"))

    async def explode(text: str) -> bool:
        raise RuntimeError("변호사 방에 못 보냄")

    sender.notify_lawyer = explode

    await pipeline.handle(make_event("질문", direct=True))
    await _settle()

    assert sender.texts[-1] == "답변"


async def _settle(rounds: int = 30) -> None:
    """Let detached alert tasks finish."""
    for _ in range(rounds):
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_escalation_reaches_the_lawyer(settings, db):
    ready_room(db)
    agent = FakeAgent("답변", escalation=Escalation(reason="형사사건", summary="폭행 사건 상담"))
    _services, sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("고소하고 싶어요"))
    await asyncio.sleep(0.05)  # follow-ups run detached

    assert any("변호사 확인 요청" in note for note in sender.lawyer_notes)


@pytest.mark.asyncio
async def test_draft_request_creates_a_pending_draft(settings, db):
    ready_room(db)
    agent = FakeAgent(
        "초안 준비하겠습니다",
        draft_request=DraftRequest(kind="내용증명", title="보증금 반환 청구", instructions="3천만원"),
    )
    _services, sender, pipeline = build(settings, db, agent)

    await pipeline.handle(make_event("내용증명 써주세요"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if db.list_drafts("pending_review"):
            break

    drafts = db.list_drafts("pending_review")
    assert len(drafts) == 1
    assert drafts[0].kind == "내용증명"
    assert drafts[0].body == "초안 본문"
    assert any("새 초안" in note for note in sender.lawyer_notes)
