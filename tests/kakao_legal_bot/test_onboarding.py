"""상담 요청마다 새 1:1 방 — 첫 이벤트에 환영·접수.

오픈프로필 링크나 듀얼번호 친구추가로 들어오면 카카오가 상담자마다 새 1:1
방을 만들어 줍니다. 서버가 할 일은 그 방의 **첫 이벤트가 무엇이든** 즉시
인사하고 접수번호를 주는 것입니다 — 상담자가 질문을 궁리하는 동안 방이
비어 있으면 나가 버립니다.
"""

from __future__ import annotations

import asyncio

import pytest

from kakao_legal_bot.app.config import Settings
from kakao_legal_bot.app.iris import IrisClient, IrisEvent
from kakao_legal_bot.app.pipeline import Pipeline
from kakao_legal_bot.app.services import Services

from .conftest import FakeAgent, FakeSender, make_event


@pytest.fixture
def wiring(settings, db):
    sender = FakeSender()
    services = Services(
        settings=settings,
        db=db,
        iris=IrisClient(settings),
        sender=sender,
        agent=FakeAgent("답변입니다"),
        semaphore=asyncio.Semaphore(2),
    )
    return Pipeline(services), services, sender


def entry_feed(room_id: str = "room-new", sender: str = "홍길동") -> IrisEvent:
    """오픈채팅 입장 피드 — 글이 아니라서 답변 대상은 아니지만 첫 접촉입니다."""
    return IrisEvent(
        room_id=room_id,
        room_name=sender,
        sender_name=sender,
        sender_id="uid-new",
        text="",
        msg_type="0",
        log_id=f"feed-{room_id}",
        created_at=0.0,
        is_direct_chat=True,
        raw={},
    )


# ── 첫 접촉 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_entering_the_room_is_enough_to_get_greeted(wiring):
    """말을 걸기 전에 인사가 먼저 — 입장 피드만으로 환영합니다."""
    pipeline, services, sender = wiring

    await pipeline.handle(entry_feed())

    assert sender.sent, "입장했는데 방이 비어 있으면 나가 버립니다"
    room_id, text = sender.sent[0]
    assert room_id == "room-new"
    assert "김변호사의 법률상담 채널" in text
    assert "접수번호" in text
    assert "AI사무장" in text
    assert services.db.get_room("room-new")["intro_sent"] == 1


@pytest.mark.asyncio
async def test_the_greeting_carries_a_consultation_number(wiring):
    pipeline, services, sender = wiring

    await pipeline.handle(entry_feed())

    consultation = services.db.get_or_create_consultation("room-new")
    assert f"접수번호 {consultation['id']}" in sender.sent[0][1]


@pytest.mark.asyncio
async def test_a_first_text_message_also_triggers_the_greeting(wiring):
    """입장 피드 없이 바로 질문부터 오는 경우 (친구추가 → 1:1)."""
    pipeline, _services, sender = wiring

    await pipeline.handle(make_event("전세금을 못 받고 있어요", room_id="room-t", direct=True))

    texts = [text for _room, text in sender.sent]
    assert any("접수번호" in text for text in texts)  # 인사가 먼저
    assert any("답변입니다" in text for text in texts)  # 답변도 왔다
    assert texts.index(next(t for t in texts if "접수번호" in t)) == 0


@pytest.mark.asyncio
async def test_each_client_gets_their_own_room_and_number(wiring):
    """상담자마다 방과 접수번호가 따로 — 이것이 전부의 목적입니다."""
    pipeline, services, sender = wiring

    await pipeline.handle(entry_feed("room-a", "김민수"))
    await pipeline.handle(entry_feed("room-b", "이영희"))

    first = services.db.get_or_create_consultation("room-a")
    second = services.db.get_or_create_consultation("room-b")
    assert first["id"] != second["id"]
    rooms = [room for room, _text in sender.sent]
    assert rooms == ["room-a", "room-b"]


@pytest.mark.asyncio
async def test_the_greeting_happens_once_even_across_events(wiring):
    pipeline, _services, sender = wiring

    await pipeline.handle(entry_feed())
    await pipeline.handle(make_event("질문입니다", room_id="room-new", direct=True))
    await pipeline.handle(make_event("추가 질문", room_id="room-new", direct=True, log_id="l2"))

    greetings = [text for _room, text in sender.sent if "접수번호" in text]
    assert len(greetings) == 1


# ── 인사하면 안 되는 곳 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_group_rooms_are_not_greeted_on_entry(wiring):
    """단체방은 호출을 받았을 때에만 — 조용히 있다가 불리면 인사합니다."""
    pipeline, _services, sender = wiring
    feed = entry_feed("room-group")
    object.__setattr__(feed, "is_direct_chat", False)

    await pipeline.handle(feed)

    assert sender.sent == []


@pytest.mark.asyncio
async def test_the_lawyers_own_room_is_not_greeted(wiring):
    pipeline, _services, sender = wiring

    await pipeline.handle(entry_feed("lawyer-room"))

    assert sender.sent == []


@pytest.mark.asyncio
async def test_the_lawyer_entering_a_room_does_not_trigger_a_greeting(wiring):
    """변호사가 듀얼번호 계정으로 먼저 말을 건 방에 봇이 끼어들면 안 됩니다."""
    pipeline, _services, sender = wiring
    feed = IrisEvent(
        room_id="room-x", room_name="", sender_name="김변호사", sender_id="lawyer-uid",
        text="", msg_type="0", log_id="f1", created_at=0.0, is_direct_chat=True, raw={},
    )

    await pipeline.handle(feed)

    assert sender.sent == []


@pytest.mark.asyncio
async def test_the_bots_own_echo_does_not_trigger_a_greeting(wiring):
    pipeline, _services, sender = wiring
    echo = IrisEvent(
        room_id="room-y", room_name="", sender_name="모아", sender_id="bot-uid",
        text="안녕하세요", msg_type="1", log_id="e1", created_at=0.0, is_direct_chat=True, raw={},
    )

    await pipeline.handle(echo)

    assert all("접수번호" not in text for _room, text in sender.sent)


@pytest.mark.asyncio
async def test_a_muted_room_stays_silent(wiring):
    pipeline, services, sender = wiring
    services.db.upsert_room("room-m", "", "direct")
    services.db.set_room_flag("room-m", "muted", 1)

    await pipeline.handle(entry_feed("room-m"))

    assert sender.sent == []


# ── 오픈채팅 1:1 인식 ────────────────────────────────────────────────────
@pytest.mark.parametrize("chat_type", ["OD", "od", "DirectChat", "OpenDirect"])
def test_open_direct_chats_parse_as_direct(chat_type):
    """오픈프로필 링크로 만들어지는 방은 OD 로 옵니다 — 그룹으로 읽으면
    호출 없이는 답하지 않아 새 상담이 조용히 죽습니다."""
    event = IrisEvent.parse(
        {"room_id": "r", "msg": "안녕하세요", "type": "1", "chat_type": chat_type}
    )
    assert event.is_direct_chat is True


def test_open_group_chats_do_not_parse_as_direct():
    event = IrisEvent.parse({"room_id": "r", "msg": "안녕", "type": "1", "chat_type": "OM"})
    assert event.is_direct_chat is False


# ── 인사말 설정 ──────────────────────────────────────────────────────────
def test_the_intro_can_be_replaced_by_a_file(tmp_path, monkeypatch):
    custom = tmp_path / "intro.md"
    custom.write_text("어서오세요! {lawyer_name} 사무실입니다. ({consult_id}번)", encoding="utf-8")
    monkeypatch.setenv("INTRO_PATH", str(custom))
    monkeypatch.setenv("LAWYER_NAME", "김변호사")

    assert Settings().intro_message(7) == "어서오세요! 김변호사 사무실입니다. (7번)"


def test_a_broken_intro_file_still_greets(tmp_path, monkeypatch):
    """괄호 실수가 첫 접촉을 침묵시키면 안 됩니다."""
    custom = tmp_path / "intro.md"
    custom.write_text("환영합니다 {없는키}", encoding="utf-8")
    monkeypatch.setenv("INTRO_PATH", str(custom))

    assert Settings().intro_message(1) == "환영합니다 {없는키}"


def test_no_file_means_the_built_in_greeting(monkeypatch):
    monkeypatch.delenv("INTRO_PATH", raising=False)
    monkeypatch.setenv("LAWYER_NAME", "김재철 변호사")
    text = Settings().intro_message(3)
    assert "김재철 변호사의 법률상담 채널" in text
    assert "접수번호 3" in text
    assert "김재철 변호사님이 채팅으로 답변" in text
    assert "비용이 청구될 수 있습니다" in text
