"""1차 가동 모드 — 제미나이 자체 지식 + 캐시된 요건사실만으로.

법률데이터베이스 없이 먼저 돕니다. 이때 세 가지가 맞아야 합니다:
빈 색인을 뒤지는 도구가 붙지 않을 것, 완성된 상담보고서가 변호사 카톡으로
갈 것, 그리고 변호사가 방 id 를 몰라도 `/등록` 한 번으로 알림이 붙을 것.
"""

from __future__ import annotations

import asyncio

import pytest

from kakao_legal_bot.app.agent import AnswerResult
from kakao_legal_bot.app.iris import IrisClient
from kakao_legal_bot.app.pipeline import Pipeline
from kakao_legal_bot.app.sender import Sender
from kakao_legal_bot.app.services import Services
from kakao_legal_bot.app.tools import IntakeAction, TurnState, build_tools

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
        semaphore=asyncio.Semaphore(2),
    )
    return Pipeline(services), services, sender


# ── 검색 도구 없이 ───────────────────────────────────────────────────────
def test_no_rag_means_no_search_tool():
    """빈 색인을 뒤지게 하느니 모델 자체 지식으로 바로 답하게 합니다."""
    names = {tool.name for tool in build_tools(state=TurnState(), rag=None, law=None)}
    assert "search_local_docs" not in names
    assert "search_law" not in names


def test_the_intake_knowledge_still_works_without_any_database():
    """요건사실·구성요건은 패키지에 실려 있어 DB 없이 돕니다 — 1차 가동의 핵심."""
    from kakao_legal_bot.app.criminal import crime_elements_for
    from kakao_legal_bot.app.knowledge import requisite_facts_for

    assert "소비대차" in requisite_facts_for("대여금")
    assert "불법영득의사" in crime_elements_for("절도")


def test_rag_enabled_switch_exists(monkeypatch):
    from kakao_legal_bot.app.config import Settings

    monkeypatch.setenv("RAG_ENABLED", "false")
    assert Settings().rag_enabled is False
    monkeypatch.delenv("RAG_ENABLED")
    assert Settings().rag_enabled is True


# ── 상담보고서 → 변호사 카톡 ─────────────────────────────────────────────
def report_agent() -> FakeAgent:
    agent = FakeAgent("상담보고서를 정리했습니다. 확인 부탁드립니다.")

    original = agent.answer

    async def answer(question, history):  # noqa: ANN001
        result: AnswerResult = await original(question, history)
        result.state.intake_actions.append(
            IntakeAction(
                kind="report",
                doc_kind="내용증명",
                case_type="대여금",
                report="# 상담보고서\n채권자 홍길동, 채무자 김철수, 3천만원, 변제기 도과",
                missing="이자 약정 여부",
            )
        )
        return result

    agent.answer = answer
    return agent


@pytest.mark.asyncio
async def test_a_finished_report_reaches_the_lawyers_kakaotalk(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=report_agent(), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)

    await pipeline.handle(make_event("네 진행할게요", room_id="room-r", direct=True))
    for _ in range(40):  # detached notify
        await asyncio.sleep(0.01)
        if any("상담보고서 완성" in note for note in sender.lawyer_notes):
            break

    note = next(note for note in sender.lawyer_notes if "상담보고서 완성" in note)
    assert "내용증명" in note and "대여금" in note
    assert "변제기 도과" in note  # 전문이 갔다
    assert "미확인 사항: 이자 약정 여부" in note
    assert "접수번호" in note


@pytest.mark.asyncio
async def test_a_very_long_report_is_trimmed_not_dropped(settings, db):
    from kakao_legal_bot.app.pipeline import _report_alert

    action = IntakeAction(kind="report", doc_kind="소장", report="가" * 5000)
    text = _report_alert(db, make_event("x", room_id="room-l"), action)
    assert len(text) < 4200
    assert "길어서 줄였습니다" in text


# ── /등록 — 변호사 셀프 등록 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_lawyer_registers_their_own_room_with_the_admin_token(settings, db):
    """방 id 를 찾아 환경변수에 넣고 재배포하는 일이 통째로 사라집니다."""
    object.__setattr__(settings, "lawyer_room_id", "")  # 아직 아무 설정 없음
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)

    await pipeline.handle(
        make_event("/등록 admin-token", room_id="my-room", sender="김재철", sender_id="kjc-1")
    )

    assert db.kv_get("lawyer_room_id") == "my-room"
    assert "kjc-1" in db.kv_get("lawyer_kakao_id")
    assert "kjc-1" in services.extra_lawyer_ids
    assert any("변호사 알림 방으로 등록" in text for text in sender.texts)

    # 이제 알림이 그 방으로 갑니다.
    real_sender = Sender(settings, db, IrisClient(settings))
    sent: list[tuple[str, str]] = []

    async def fake_send(room_id, text, *, record_role="bot"):  # noqa: ANN001
        sent.append((room_id, text))
        return True

    real_sender.send = fake_send
    assert await real_sender.notify_lawyer("테스트 알림") is True
    assert sent == [("my-room", "테스트 알림")]


@pytest.mark.asyncio
async def test_a_wrong_token_is_met_with_silence(settings, db):
    object.__setattr__(settings, "lawyer_room_id", "")
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)

    await pipeline.handle(make_event("/등록 틀린토큰", room_id="evil-room", direct=True))

    assert db.kv_get("lawyer_room_id") == ""
    # 오답에 반응하면 토큰을 맞혀 보라고 알려주는 셈 — 등록 확인 메시지는 없어야 한다
    assert not any("등록" in text for text in sender.texts)


@pytest.mark.asyncio
async def test_registration_survives_a_restart(settings, db):
    """kv 에 저장되므로 재시작해도 변호사 권한이 남습니다."""
    db.kv_set("lawyer_kakao_id", "kjc-1")

    from kakao_legal_bot.app.trigger import is_lawyer

    event = make_event("아무 말", sender_id="kjc-1")
    loaded = {v for v in db.kv_get("lawyer_kakao_id").split(",") if v}  # build_services 가 하는 일
    assert is_lawyer(event, settings, loaded) is True
    assert is_lawyer(event, settings) is False  # 설정만으로는 모른다


@pytest.mark.asyncio
async def test_registered_lawyer_can_use_lawyer_commands(settings, db):
    object.__setattr__(settings, "lawyer_room_id", "")
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)
    await pipeline.handle(
        make_event("/등록 admin-token", room_id="my-room", sender="김재철", sender_id="kjc-1")
    )

    await pipeline.handle(
        make_event("/상태", room_id="my-room", sender="김재철", sender_id="kjc-1", log_id="s1")
    )

    assert any("모아 상태" in text for text in sender.texts)


@pytest.mark.asyncio
async def test_the_lawyers_registered_room_is_not_treated_as_a_consultation(settings, db):
    """등록된 방에 환영 인사·접수번호가 가면 이상합니다."""
    object.__setattr__(settings, "lawyer_room_id", "")
    sender = FakeSender()
    services = Services(
        settings=settings, db=db, iris=IrisClient(settings), sender=sender,
        agent=FakeAgent("답변"), semaphore=asyncio.Semaphore(2),
    )
    pipeline = Pipeline(services)
    await pipeline.handle(
        make_event("/등록 admin-token", room_id="my-room", sender="김재철", sender_id="kjc-1")
    )
    sender.sent.clear()

    await pipeline.handle(
        make_event("메모입니다", room_id="my-room", sender="김재철", sender_id="kjc-1", log_id="m1")
    )

    assert not any("접수번호" in text for text in sender.texts)
